# coding=utf-8
import datetime
import logging
import time
import urllib2
from json import loads
from StringIO import StringIO

import pytz
import requests
from allauth.account import app_settings as account_app_settings
from allauth.account.adapter import get_adapter
from allauth.account.forms import SignupForm
from allauth.account.models import EmailAddress
from allauth.account.utils import complete_signup
from allauth.socialaccount.models import SocialAccount
from dateutil import parser
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry, Point
from django.contrib.gis.measure import D
from django.contrib.sessions.models import Session
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.db import IntegrityError
from django.db.models import (BooleanField, Case, Count, IntegerField, Q,
                              Value, When)
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import activate
from django.views.decorators.cache import never_cache
from oauth2client import client, crypt
from PIL import ExifTags, Image
from rest_framework import authentication, exceptions, generics
from rest_framework.decorators import (api_view, authentication_classes,
                                       parser_classes, permission_classes)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler
from sorl.thumbnail import get_thumbnail

from project.ajapaik import forms, serializers
from project.ajapaik.models import Album, Licence, Photo, PhotoLike, Profile
from project.ajapaik.settings import (API_DEFAULT_NEARBY_MAX_PHOTOS,
                                      API_DEFAULT_NEARBY_PHOTOS_RANGE,
                                      FACEBOOK_APP_ID, FACEBOOK_APP_SECRET,
                                      GOOGLE_CLIENT_ID, MEDIA_ROOT, TIME_ZONE)

log = logging.getLogger(__name__)

# Response statuses
RESPONSE_STATUSES = {
    'OK': 0,  # no error
    'UNKNOWN_ERROR': 1,  # unknown error
    'INVALID_PARAMETERS': 2,  # invalid input parameter
    'MISSING_PARAMETERS': 3,  # missing input parameter
    'ACCESS_DENIED': 4,  # access denied
    'SESSION_REQUIRED': 5,  # session is required
    'SESSION_EXPIRED': 6,  # session is expired
    'INVALID_SESSION': 7,  # session is invalid
    'USER_ALREADY_EXISTS': 8,  # user exists in the DB already
    'INVALID_APP_VERSION': 9,  # application version is not supported
    'MISSING_USER': 10,  # user does not exist
    'INVALID_PASSWORD': 11,  # wrong password for existing user
}


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    # TODO: Error handling
    if response is not None:
        response.data['error'] = 7

    return response


class CustomAuthentication(authentication.BaseAuthentication):
    @parser_classes((FormParser,))
    def authenticate(self, request):
        cat_auth_form = forms.APIAuthForm(request.data)
        user = None
        if cat_auth_form.is_valid():
            lang = cat_auth_form.cleaned_data['_l']
            if lang:
                activate(lang)
            user_id = cat_auth_form.cleaned_data['_u']
            session_id = cat_auth_form.cleaned_data['_s']
            if not session_id or not user_id:
                return None
            try:
                Session.objects.get(session_key=session_id)
                user = User.objects.get(pk=user_id)
            except (User.DoesNotExist, Session.DoesNotExist):
                raise exceptions.AuthenticationFailed('No user/session')

        return user, None


class CustomAuthenticationMixin(object):
    authentication_classes = (CustomAuthentication,)
    permission_classes = (IsAuthenticated,)


class CustomParsersMixin(object):
    parser_classes = (FormParser, MultiPartParser,)


class Login(CustomParsersMixin, APIView):
    '''
    API endpoint to login user.
    '''

    permission_classes = (AllowAny,)

    def _authenticate_by_email(self, email, password):
        '''
        Authenticate user with email and password.
        '''
        user = authenticate(email=email, password=password)
        if user is not None and not user.is_active:
            # We found user but this user is disabled. "authenticate" does't
            # checking is user is disabled(at least in Django 1.8).
            return
        return user

    def _authenticate_with_google(self, user_id):
        '''
        Returns user by google account ID.
        '''
        try:
            return SocialAccount.objects.get(
                provider='google',
                uid=user_id
            ).user
        except MultipleObjectsReturned:
            log.warning('Multiple social accounts returned while trying to '
                        'login with google user id. It shouldn\'t happen. '
                        'Provider and social id must be unique togeser. '
                        'Check social account with ID: %d', user_id)
            return
        except ObjectDoesNotExist:
            return

    def _authenticate_with_facebook(self, user_id):
        '''
        Returns user by facebook user ID.
        '''
        try:
            return SocialAccount.objects.get(
                provider='facebook',
                uid=user_id
            ).user
        except MultipleObjectsReturned:
            log.warning('Multiple social accounts returned while trying to '
                        'login with facebook user id. It shouldn\'t happen. '
                        'Provider and social id must be unique togeser. '
                        'Check social account with ID: %d', user_id)
            return
        except ObjectDoesNotExist:
            return

    def post(self, request, format=None):
        form = forms.APILoginForm(request.data)
        if form.is_valid():
            login_type = form.cleaned_data['type']
            if login_type == forms.APILoginForm.LOGIN_TYPE_AJAPAIK:
                email = form.cleaned_data['username']
                password = form.cleaned_data['password']
                if password is None:
                    return Response({
                        'error': RESPONSE_STATUSES['MISSING_PARAMETERS'],
                        'id': None,
                        'session': None,
                        'expires': None,
                    })
                user = self._authenticate_by_email(email, password)
            elif login_type == forms.APILoginForm.LOGIN_TYPE_GOOGLE:
                email = form.cleaned_data['username']
                user = self._authenticate_with_google(email)
            elif login_type == forms.APILoginForm.LOGIN_TYPE_FACEBOOK:
                facebook_user_id = form.cleaned_data['username']
                user = self._authenticate_with_facebook(facebook_user_id)
            elif login_type == forms.APILoginForm.LOGIN_TYPE_AUTO:
                # Deprecated. Keeped for back compatibility.
                user = None

            if user is None:
                # We can't authenticate user with provided login/password pair.
                return Response({
                    'error': RESPONSE_STATUSES['MISSING_USER'],
                    'id': None,
                    'session': None,
                    'expires': None,
                })

            get_adapter(request).login(request, user)
            if not request.session.session_key:
                request.session.save()

            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'id': user.id,
                'session': request.session.session_key,
                'expires': request.session.get_expiry_age(),
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'id': None,
                'session': None,
                'expires': None,
            })


class Register(CustomParsersMixin, APIView):
    '''
    API endpoint to register user.
    '''

    permission_classes = (AllowAny,)

    def post(self, request, format=None):
        form = forms.APIRegisterForm(request.data)
        if form.is_valid():
            registration_type = form.cleaned_data['type']
            if registration_type == forms.APIRegisterForm.REGISTRATION_TYPE_AJAPAIK:
                email = form.cleaned_data['username']
                password = form.cleaned_data['password']
                firstname = form.cleaned_data['firstname']
                lastname = form.cleaned_data['lastname']

                account_signup_form = SignupForm(data={
                    'email': email,
                    'password1': password,
                    'password2': password,
                })
                if not account_signup_form.is_valid():
                    return Response({
                        'error': RESPONSE_STATUSES['UNKNOWN_ERROR'],
                        'id': None,
                        'session': None,
                        'expires': None,
                    })
                new_user = account_signup_form.save(request._request)

                new_user.first_name = firstname
                new_user.last_name = lastname
                new_user.save()

                complete_signup(
                    request._request,
                    new_user,
                    account_app_settings.EMAIL_VERIFICATION,
                    None
                )
                return Response({
                    'error': RESPONSE_STATUSES['OK'],
                    'id': new_user.id,
                    'session': None,
                    'expires': None,
                })
            elif registration_type == forms.APIRegisterForm.REGISTRATION_TYPE_GOOGLE:
                return Response({
                    'error': RESPONSE_STATUSES['OK'],
                    'id': None,
                    'session': None,
                    'expires': None,
                })
            elif registration_type == forms.APIRegisterForm.REGISTRATION_TYPE_FACEBOOK:
                return Response({
                    'error': RESPONSE_STATUSES['OK'],
                    'id': None,
                    'session': None,
                    'expires': None,
                })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'id': None,
                'session': None,
                'expires': None,
            })



@api_view(['POST'])
@parser_classes((FormParser,))
@authentication_classes((CustomAuthentication,))
@permission_classes((IsAuthenticated,))
def api_logout(request):
    try:
        session_id = request.data['_s']
    except KeyError:
        return Response({'error': 4})
    try:
        Session.objects.get(pk=session_id).delete()
        return Response({'error': 0})
    except ObjectDoesNotExist:
        return Response({'error': 2})


@never_cache
@permission_classes((AllowAny,))
def api_album_thumb(request, album_id, thumb_size=250):
    a = get_object_or_404(Album, id=album_id)
    random_image = a.photos.order_by('?').first()
    if not random_image:
        for sa in a.subalbums.exclude(atype=Album.AUTO):
            random_image = sa.photos.order_by('?').first()
            if random_image:
                break
    size_str = str(thumb_size)
    thumb_str = size_str + 'x' + size_str
    try:
        im = get_thumbnail(random_image.image, thumb_str, upscale=False)
    except AttributeError:
        im = urllib2.urlopen('http://rlv.zcache.com/doge_sticker-raf4b2dbd11ec4a7992e8bf94601ace75_v9wth_8byvr_512.jpg')
    content = im.read()
    response = HttpResponse(content, content_type='image/jpg')

    return response


class AlbumList(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to get albums that: public and not empty, or albums that
    overseeing by current user(can be empty).
    '''
    def post(self, request, format=None):
        albums = Album.objects.filter(
            Q(is_public=True, photos__isnull=False)
            | Q(profile=request.get_user().profile, atype=Album.CURATED)
        ).order_by('-created')
        albums = serializers.AlbumDetailsSerializer.annotate_albums(albums)

        return Response({
            'error': RESPONSE_STATUSES['OK'],
            'albums': serializers.AlbumDetailsSerializer(
                instance=albums,
                many=True,
                context={'request': request}
            ).data
        })


class AlbumNearestPhotos(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to retrieve album photos(if album is specified else just
    photos) in specified radius.
    '''
    def post(self, request, format=None):
        form = forms.ApiAlbumNearestPhotosForm(request.data)
        if form.is_valid():
            album = form.cleaned_data["id"]
            nearby_range = form.cleaned_data["range"] or API_DEFAULT_NEARBY_PHOTOS_RANGE
            ref_location = Point(
                round(form.cleaned_data["longitude"], 4),
                round(form.cleaned_data["latitude"], 4)
            )
            if album:
                photos = Photo.objects.filter(
                    Q(albums=album)
                    | (Q(albums__subalbum_of=album)
                       & ~Q(albums__atype=Album.AUTO)),
                    rephoto_of__isnull=True
                ).filter(
                    lat__isnull=False,
                    lon__isnull=False,
                    geography__distance_lte=(ref_location, D(m=nearby_range))
                ) \
                    .distance(ref_location) \
                    .order_by('distance')[:API_DEFAULT_NEARBY_MAX_PHOTOS]

                return Response({
                    'error': RESPONSE_STATUSES['OK'],
                    'photos': serializers.AlbumSerializer(
                        instance=album,
                        context={
                            'request': request,
                            'photos': photos
                        }
                    ).data
                })
            else:
                photos = Photo.objects.filter(
                    lat__isnull=False,
                    lon__isnull=False,
                    rephoto_of__isnull=True,
                    geography__distance_lte=(ref_location, D(m=nearby_range))
                ) \
                    .distance(ref_location) \
                    .order_by('distance')[:API_DEFAULT_NEARBY_MAX_PHOTOS]

                photos = serializers.PhotoSerializer.annotate_photos(
                    photos,
                    request.user.profile
                )

                return Response({
                    'error': RESPONSE_STATUSES['OK'],
                    'photos': serializers.PhotoSerializer(
                        instance=photos,
                        many=True,
                        context={'request': request}
                    ).data
                })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'photos': []
            })


class AlbumDetails(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to retrieve album details and details of this album photos.
    '''
    def post(self, request, format=None):
        form = forms.ApiAlbumStateForm(request.data)
        if form.is_valid():
            album = form.cleaned_data["id"]
            photos = Photo.objects.filter(
                Q(albums=album)
                | (Q(albums__subalbum_of=album)
                   & ~Q(albums__atype=Album.AUTO)),
                rephoto_of__isnull=True
            )
            response_data = {
                'error': RESPONSE_STATUSES['OK']
            }
            response_data.update(serializers.AlbumSerializer(
                instance=album,
                context={
                    'request': request,
                    'photos': photos
                }
            ).data)
            return Response(response_data)
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS']
            })


def _crop_image(img, scale_factor):
    new_size = tuple([int(x * scale_factor) for x in img.size])
    left = (img.size[0] - new_size[0]) / 2
    top = (img.size[1] - new_size[1]) / 2
    right = (img.size[0] + new_size[0]) / 2
    bottom = (img.size[1] + new_size[1]) / 2
    img = img.crop((left, top, right, bottom))

    return img


def _fill_missing_pixels(img, scale_factor):
    new_size = tuple([int(x * scale_factor) for x in img.size])
    x0 = (new_size[0] - img.size[0]) / 2
    y0 = (new_size[1] - img.size[1]) / 2
    new_img = Image.new('RGB', new_size, color=(255, 255, 255))
    new_img.paste(img, (x0, y0))

    return new_img


@api_view(['POST'])
@parser_classes((FormParser, MultiPartParser))
@authentication_classes((CustomAuthentication,))
@permission_classes((IsAuthenticated,))
def api_photo_upload(request):
    profile = request.user.profile
    upload_form = forms.ApiPhotoUploadForm(request.data, request.FILES)
    content = {
        'error': 0
    }
    if upload_form.is_valid():
        original_photo = upload_form.cleaned_data['id']
        lat = upload_form.cleaned_data['latitude']
        lng = upload_form.cleaned_data['longitude']
        geography = None
        if lat and lng:
            geography = Point(x=lng, y=lat, srid=4326)
        parsed_date = parser.parse(upload_form.cleaned_data['date'], dayfirst=True, ignoretz=True)
        # This workaround assumes we're based in a +something timezone, Europe/Tallinn for example,
        # since the date is sent in as a dd-mm-yyyy string, we don't want any timezone-aware magic
        tz = pytz.timezone(TIME_ZONE)
        dt = datetime.datetime.utcnow()
        offset_seconds = tz.utcoffset(dt).seconds
        offset_hours = offset_seconds / 3600.0
        new_rephoto = Photo(
            image_unscaled=upload_form.cleaned_data['original'],
            image=upload_form.cleaned_data['original'],
            rephoto_of=original_photo,
            lat=lat,
            lon=lng,
            date=datetime.datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour=0 + int(offset_hours)),
            geography=geography,
            gps_accuracy=upload_form.cleaned_data['accuracy'],
            gps_fix_age=upload_form.cleaned_data['age'],
            cam_scale_factor=upload_form.cleaned_data['scale'],
            cam_yaw=upload_form.cleaned_data['yaw'],
            cam_pitch=upload_form.cleaned_data['pitch'],
            cam_roll=upload_form.cleaned_data['roll'],
            flip=upload_form.cleaned_data['flip'],
            licence=Licence.objects.filter(url='https://creativecommons.org/licenses/by/2.0/').first(),
            user=profile,
        )
        new_rephoto.light_save()
        img = Image.open(MEDIA_ROOT + '/' + str(new_rephoto.image))
        img_unscaled = Image.open(MEDIA_ROOT + '/' + str(new_rephoto.image_unscaled))
        try:
            image_orientation = img._getexif()[
                next(
                    (key for key, value in ExifTags.TAGS.items() if value == 'Orientation'),
                    None
                )
            ]
        except KeyError:
            image_orientation = None
        if image_orientation == 3:
            img = img.rotate(180, expand=True)
            img_unscaled = img.rotate(180, expand=True)
            img.save(MEDIA_ROOT + '/' + str(new_rephoto.image), 'JPEG', quality=95)
            img_unscaled.save(MEDIA_ROOT + '/' + str(new_rephoto.image_unscaled), 'JPEG', quality=95)
        elif image_orientation == 6:
            img = img.rotate(270, expand=True)
            img_unscaled = img_unscaled.rotate(270, expand=True)
            img.save(MEDIA_ROOT + '/' + str(new_rephoto.image), 'JPEG', quality=95)
            img_unscaled.save(MEDIA_ROOT + '/' + str(new_rephoto.image_unscaled), 'JPEG', quality=95)
        elif image_orientation == 8:
            img = img.rotate(90, expand=True)
            img_unscaled = img_unscaled.rotate(90, expand=True)
            img.save(MEDIA_ROOT + '/' + str(new_rephoto.image), 'JPEG', quality=95)
            img_unscaled.save(MEDIA_ROOT + '/' + str(new_rephoto.image_unscaled), 'JPEG', quality=95)
        if upload_form.cleaned_data['scale']:
            img = Image.open(MEDIA_ROOT + '/' + str(new_rephoto.image))
            rounded_scale = round(float(upload_form.cleaned_data['scale']), 6)
            output_file = StringIO()
            if rounded_scale < 1:
                cropped_image = _crop_image(img, rounded_scale)
                cropped_image.save(output_file, 'JPEG', quality=95)
            elif rounded_scale > 1:
                filled_image = _fill_missing_pixels(img, rounded_scale)
                filled_image.save(output_file, 'JPEG', quality=95)
            new_rephoto.image.delete()
            new_rephoto.image.save(str(new_rephoto.image), ContentFile(output_file.getvalue()))
        content['id'] = new_rephoto.pk
        original_photo.latest_rephoto = new_rephoto.created
        if not original_photo.first_rephoto:
            original_photo.first_rephoto = new_rephoto.created
        original_photo.light_save()
        profile.update_rephoto_score()
        profile.save()
    else:
        content['error'] = 2

    return Response(content)


@api_view(['POST'])
@parser_classes((FormParser,))
@authentication_classes((CustomAuthentication,))
@permission_classes((IsAuthenticated,))
def api_user_me(request):
    profile = request.user.profile
    content = {
        'error': 0,
        'state': str(int(round(time.time() * 1000)))
    }
    form = forms.ApiUserMeForm(request.data)
    if form.is_valid():
        content['name'] = profile.get_display_name()
        content['rephotos'] = profile.photos.filter(rephoto_of__isnull=False).count()
        general_user_leaderboard = Profile.objects.filter(score__gt=0).order_by('-score')
        general_user_rank = 0
        for i in range(0, len(general_user_leaderboard)):
            if general_user_leaderboard[i].user_id == profile.user_id:
                general_user_rank = (i + 1)
                break
        content['rank'] = general_user_rank
    else:
        content['error'] = 2

    return Response(content)


class PhotoDetails(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to retrieve photo details.
    '''
    def post(self, request, format=None):
        form = forms.ApiPhotoStateForm(request.data)
        if form.is_valid():
            user_profile = request.user.profile
            photo = Photo.objects.filter(
                pk=form.cleaned_data['id'],
                rephoto_of__isnull=True
            )
            if photo:
                photo = serializers.PhotoSerializer.annotate_photos(
                    photo,
                    request.user.profile
                ).first()
                response_data = {'error': RESPONSE_STATUSES['OK']}
                response_data.update(
                    serializers.PhotoSerializer(
                        instance=photo, context={'request': request}
                    ).data
                )
                return Response(response_data)
            else:
                return Response({
                    'error': RESPONSE_STATUSES['INVALID_PARAMETERS']
                })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS']
            })


class ToggleUserFavoritePhoto(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to un/like photos by user.
    '''
    def post(self, request, format=None):
        form = forms.ApiToggleFavoritePhotoForm(request.data)
        if form.is_valid():
            photo = form.cleaned_data['id']
            is_favorited = form.cleaned_data['favorited']
            user_profile = request.user.profile

            if is_favorited:
                try:
                    # User already have this photo in favorites.
                    # Nothing to do here.
                    PhotoLike.objects.get(photo=photo, profile=user_profile)
                except PhotoLike.DoesNotExist:
                    # User haven't this photo in favorites.
                    # Creating like.
                    photo_like = PhotoLike.objects.create(
                        photo=photo, profile=user_profile
                    )
                    photo_like.save()
            else:
                try:
                    # User already have this photo in favorites.
                    # Removing it.
                    photo_like = PhotoLike.objects.get(
                        photo=photo, profile=user_profile
                    )
                    photo_like.delete()
                except PhotoLike.DoesNotExist:
                    # User haven't this photo in favorites.
                    # Nothing to do here.
                    pass
            return Response({'error': RESPONSE_STATUSES['OK']})
        else:
            return Response({'error': RESPONSE_STATUSES['INVALID_PARAMETERS']})


class UserFavoritePhotoList(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to retrieve user liked photos sorted by distance to specified
    location.
    '''
    def post(self, request, format=None):
        form = forms.ApiFavoritedPhotosForm(request.data)
        if form.is_valid():
            user_profile = request.get_user().profile
            latitude = form.cleaned_data['latitude']
            longitude = form.cleaned_data['longitude']
            requested_location = GEOSGeometry(
                'POINT({} {})'.format(longitude, latitude),
                srid=4326
            )
            photos = Photo.objects.filter(likes__profile=user_profile) \
                .distance(requested_location) \
                .order_by('distance')
            photos = serializers.PhotoWithDistanceSerializer.annotate_photos(
                photos,
                request.user.profile
            )
            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'photos': serializers.PhotoWithDistanceSerializer(
                    instance=photos, many=True, context={'request': request}
                ).data,
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'photos': [],
            })


class PhotosSearch(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to search for photos by given phrase.
    '''
    def post(self, request, format=None):
        form = forms.ApiPhotoSearchForm(request.data)
        if form.is_valid():
            search_phrase = form.cleaned_data['query']
            rephotos_only = form.cleaned_data['rephotosOnly']

            search_results = forms.HaystackPhotoSearchForm({
                'q': search_phrase
            }).search()

            photos = Photo.objects.filter(
                id__in=[item.pk for item in search_results],
            )
            if rephotos_only:
                photos = photos.filter(
                    rephoto_of__isnull=False
                )
            photos = serializers.PhotoSerializer.annotate_photos(
                photos,
                request.user.profile
            )

            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'photos': serializers.PhotoSerializer(
                    instance=photos,
                    many=True,
                    context={'request': request}
                ).data
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'photos': []
            })


class PhotosInAlbumSearch(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to search for photos in album by given phrase.
    '''
    def post(self, request, format=None):
        form = forms.ApiPhotoInAlbumSearchForm(request.data)
        if form.is_valid():
            search_phrase = form.cleaned_data['query']
            album = form.cleaned_data['albumId']
            rephotos_only = form.cleaned_data['rephotosOnly']

            search_results = forms.HaystackPhotoSearchForm({
                'q': search_phrase
            }).search()

            # "rephoto_of__albums=album" added bacause to rephoto not setted
            # albums of original photo.
            photos = Photo.objects.filter(
                Q(id__in=[item.pk for item in search_results])
                & (Q(albums=album) | Q(rephoto_of__albums=album))
            )
            if rephotos_only:
                photos = photos.filter(
                    rephoto_of__isnull=False
                )
            photos = serializers.PhotoSerializer.annotate_photos(
                photos,
                request.user.profile
            )

            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'photos': serializers.PhotoSerializer(
                    instance=photos,
                    many=True,
                    context={'request': request}
                ).data
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'photos': []
            })


class UserRephotosSearch(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to search for rephotos done by user by given search phrase.
    '''
    def post(self, request, format=None):
        form = forms.ApiUserRephotoSearchForm(request.data)
        if form.is_valid():
            search_phrase = form.cleaned_data['query']
            user_profile = request.user.profile

            search_results = forms.HaystackPhotoSearchForm({
                'q': search_phrase
            }).search()

            photos = Photo.objects.filter(
                id__in=[item.pk for item in search_results],
                rephoto_of__isnull=False,
                user=user_profile
            )
            photos = serializers.PhotoSerializer.annotate_photos(
                photos,
                user_profile
            )

            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'photos': serializers.PhotoSerializer(
                    instance=photos,
                    many=True,
                    context={'request': request}
                ).data
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'photos': []
            })


class AlbumsSearch(CustomAuthenticationMixin, CustomParsersMixin, APIView):
    '''
    API endpoint to search for albums by given search phrase.
    '''
    def post(self, request, format=None):
        form = forms.ApiAlbumSearchForm(request.data)
        if form.is_valid():
            search_phrase = form.cleaned_data['query']
            user_profile = request.user.profile

            search_results = forms.HaystackAlbumSearchForm({
                'q': search_phrase
            }).search()

            albums = Album.objects.filter(
                id__in=[item.pk for item in search_results]
            )
            albums = serializers.AlbumDetailsSerializer.annotate_albums(albums)

            return Response({
                'error': RESPONSE_STATUSES['OK'],
                'albums': serializers.AlbumDetailsSerializer(
                    instance=albums,
                    many=True,
                    context={'request': request}
                ).data
            })
        else:
            return Response({
                'error': RESPONSE_STATUSES['INVALID_PARAMETERS'],
                'albums': []
            })
