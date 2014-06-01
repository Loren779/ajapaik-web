# encoding: utf-8

from django.shortcuts import render_to_response
from django.template import RequestContext
from django.contrib import messages
from django.contrib.sites.models import Site
from django.http import HttpResponse
from django.utils import simplejson as json
from django.utils.translation import ugettext as _
from django.shortcuts import redirect, get_object_or_404
from django.conf import settings

from django.core.files import File
from django.core.files.base import ContentFile
from django.core.exceptions import ObjectDoesNotExist

from project.models import Photo, City, Profile, Source, Device
from project.forms import GeoTagAddForm, CitySelectForm
from sorl.thumbnail import get_thumbnail
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
from PIL.ExifTags import TAGS, GPSTAGS
from time import gmtime, strftime, strptime

from pprint import pprint
from django.db import models

import get_next_photos_to_geotag
import random
import datetime
import urllib

from django.forms.forms import Form
from django.forms.fields import ChoiceField


def _convert_to_degress(value):
    d0 = value[0][0]
    d1 = value[0][1]
    d = float(d0) / float(d1)

    m0 = value[1][0]
    m1 = value[1][1]
    m = float(m0) / float(m1)

    s0 = value[2][0]
    s1 = value[2][1]
    s = float(s0) / float(s1)

    return d + (m / 60.0) + (s / 3600.0)
    
def _get_exif_data(img):
    try:
        exif = img._getexif()
    except (AttributeError, IOError, KeyError, IndexError):
        exif = None
    if (exif is None):
        return None

    exif_data = {}
    for (tag, value) in exif.items():
        decoded = TAGS.get(tag, tag)
        if decoded == "GPSInfo":
            for t in value:
                sub_decoded = GPSTAGS.get(t, t)
                exif_data[decoded+'.'+sub_decoded] = value[t]

        elif (len(str(value)) < 50):
            exif_data[decoded] = value
        else:
            exif_data[decoded] = None
            
    return exif_data

class FilterSpecCollection(object): # selectbox type, choice based
    def __init__(self, qs, params):
        self.qs = qs
        self.params = params
        self.filters = []
        self.filters_by_name = {}
        
    def register(self, filter_spec, field):
        filter_obj = filter_spec(self.params, field)
        self.filters.append(filter_obj)
        self.filters_by_name[filter_obj.get_slug_name()] = filter_obj
        
    def out(self):
        for f in self.filters:
            pass
    
    def get_filtered_qs(self):
        for item in self.filters:
            qs_filter = item.get_qs_filter()
            if qs_filter:
                self.qs = self.qs.filter(**dict(item.get_qs_filter()))
        return self.qs
    
    def get_form(self):
        class DynaForm(Form):
            def __init__(self, *args, **kwargs):
                args = list(args)
                filters = args.pop(0)
                
                super(DynaForm, self).__init__(*args, **kwargs)
                for item in filters:
                    choices = [(i['query_string'], i['display']) for i in list(item.choices())]
                    self.fields[item.get_slug_name()] = ChoiceField(choices=choices, label=item.get_label())
        
        initial = {}
        for item in self.filters:
            selected = [i['query_string'] for i in item.choices() if i['selected']] or ["",]
            initial[item.get_slug_name()] = selected.pop(0)
        
        return DynaForm(self.filters, initial=initial)

class FilterSpec(object):
    def get_qs_filter(self):
        for title, param_dict in self.links:
            if param_dict == self.selected_params:
                return self.selected_params
        return False
    
    def choices(self):
        for title, param_dict in self.links:
            yield {'selected': self.selected_params == param_dict,
                   'query_string': urllib.urlencode(param_dict),
                   'display': title
                   }
                                      
class DateFieldFilterSpec(FilterSpec):
    def __init__(self, params, field_path):
        self.field_path = field_path
        self.field_generic = '%s__' % field_path
        self.selected_params = dict([(k, v) for k, v in params.items() if k.startswith(self.field_generic)])
        
        today = datetime.date.today()
        one_week_ago = today - datetime.timedelta(days=7)
        one_month_ago = today - datetime.timedelta(days=30)
        today_str = today.strftime('%Y-%m-%d 23:59:59')
        
        self.links = (
            (_('All pictures'), {
                '%s__gte' % self.field_path: "",
                '%s__lte' % self.field_path: "",            
            }),
            (_('Added last week'), {
                '%s__gte' % self.field_path: one_week_ago.strftime('%Y-%m-%d'),
                '%s__lte' % self.field_path: today_str,
            }),
            (_('Added last month'), {
                '%s__gte' % self.field_path: one_month_ago.strftime('%Y-%m-%d'),
                '%s__lte' % self.field_path: today_str,
            }),
        )

    def get_slug_name(self):
        return u'creation_date_filter'

    def get_option_object(self):
        return None

    def get_label(self):
        return _('Choose range')


class CityLookupFilterSpec(FilterSpec):
    def __init__(self, params, field_path):
        self.field_path = field_path
        self.field_generic = '%s__' % field_path
        self.selected_params = dict([(k, v) for k, v in params.items() if k.startswith(self.field_generic)])
        self.lookup = '%s__pk' % self.field_path
            
        self.links = []
        for city in City.objects.filter(lat__isnull=False, lon__isnull=False):
            self.links.append((city.name, {self.lookup: u'%s' % city.pk}))
        
        # Initial and default value
        if not self.get_qs_filter():
            self.selected_params = {self.lookup: u'%s' % settings.DEFAULT_CITY_ID}
        
    def get_option_object(self):
        return City.objects.get(**dict({"pk": self.selected_params[self.lookup]}))
    
    def get_slug_name(self):
        return u'city_lookup_filter'
    
    def get_label(self):
        return _('Choose city')

class SourceLookupFilterSpec(FilterSpec):
    def __init__(self, params, field_path):
        self.field_path = field_path
        self.field_generic = '%s__' % field_path
        self.selected_params = dict([(k, v) for k, v in params.items() if k.startswith(self.field_generic)])
        self.lookup = '%s__eq' % self.field_path
            
        self.links = []
        self.links.append(('--', ''))
        for source in Source.objects.all():
            self.links.append((source.description, {self.lookup: u'%s' % source.pk}))
    
    def get_option_object(self):
        return Source.objects.get(**dict({"pk": self.selected_params[self.lookup]}))
    
    def get_slug_name(self):
        return u'source_lookup_filter'
    
    def get_label(self):
        return _('Choose source')

def handle_uploaded_file(f):
    return ContentFile(f.read())

def photo_upload(request, photo_id):
    photo = get_object_or_404(Photo, pk=photo_id)
    new_id = 0
    if request.method == 'POST':
        profile = request.get_user().get_profile()
        
        if 'fb_access_token' in request.POST:
            token = request.POST.get('fb_access_token')
            profile, fb_data = Profile.facebook.get_user(token)
            if profile is None:
                user = request.get_user() # will create user on-demand
                profile = user.get_profile()
                
                # update user info
                profile.update_from_fb_data(token, fb_data)
        
        # get the latest uploaded rephoto
        latest_upload = Photo.objects.filter(rephoto_of=photo)
        previous_uploader = None
        if latest_upload:
            previous_uploader = latest_upload.values('user').order_by('-id')[:1].get()
        
        if 'user_file[]' in request.FILES.keys():
            for f in request.FILES.getlist('user_file[]'):
                fileobj = handle_uploaded_file(f)
                data = request.POST
                re_photo = Photo(
                    rephoto_of=photo,
                    city=photo.city,
                    description=data.get('description', photo.description),
                    lat=data.get('lat', None),
                    lon=data.get('lon', None),
                    date_text=data.get('date_text', None),
                    user=profile,
                    cam_scale_factor=data.get('scale_factor'),
                    cam_yaw=data.get('yaw'),
                    cam_pitch=data.get('pitch'),
                    cam_roll=data.get('roll'),
                )
                re_photo.save()
                re_photo.image.save(f.name, fileobj)
                new_id = re_photo.pk
                
                img = Image.open('/var/garage/' + str(re_photo.image))
                exif_data = _get_exif_data(img)
                if (exif_data):
                    if ('GPSInfo.GPSLatitudeRef' in exif_data and 'GPSInfo.GPSLatitude' in exif_data and 'GPSInfo.GPSLongitudeRef' in exif_data and 'GPSInfo.GPSLongitude' in exif_data):
                        gps_latitude_ref = exif_data.get('GPSInfo.GPSLatitudeRef')
                        gps_latitude = exif_data.get('GPSInfo.GPSLatitude')
                        gps_longitude_ref = exif_data.get('GPSInfo.GPSLongitudeRef')
                        gps_longitude = exif_data.get('GPSInfo.GPSLongitude')

                        lat = _convert_to_degress(gps_latitude)
                        if gps_latitude_ref != "N":
                            lat = 0 - lat

                        lon = _convert_to_degress(gps_longitude)
                        if gps_longitude_ref != "E":
                            lon = 0 - lon

                        re_photo.lat = lat
                        re_photo.lon = lon
                        re_photo.save()

                    if ('Make' in exif_data or 'Model' in exif_data or 'LensMake' in exif_data or 'LensModel' in exif_data or 'Software' in exif_data):
                        camera_make = exif_data.get('Make')
                        camera_model = exif_data.get('Model')
                        lens_make = exif_data.get('LensMake')
                        lens_model = exif_data.get('LensModel')
                        software = exif_data.get('Software')
                        try:
                            # find existing device configuration
                            device=Device.objects.get(camera_make=camera_make, camera_model=camera_model, lens_make=lens_make, lens_model=lens_model, software=software)
                        except ObjectDoesNotExist:
                            # create new device configuration
                            device=Device(camera_make=camera_make, camera_model=camera_model, lens_make=lens_make, lens_model=lens_model, software=software)
                            device.save()

                        re_photo.device = device
                        re_photo.save()

                    if ('DateTimeOriginal' in exif_data):
                        date_taken = exif_data.get('DateTimeOriginal')
                        try:
                            parsed_time = strptime(date_taken, "%Y:%m:%d %H:%M:%S")
                        except (ValueError):
                            parsed_time = None
                        if (parsed_time):
                            parsed_time = strftime("%H:%M:%S", parsed_time)

                        # ignore default camera dates
                        if (parsed_time and parsed_time != '12:00:00' and parsed_time != '00:00:00'):
                            try:
                                parsed_date = strptime(date_taken, "%Y:%m:%d %H:%M:%S")
                            except (ValueError):
                                parsed_date = None
                            if (parsed_date):
                                re_photo.date = strftime("%Y-%m-%d", parsed_date)
                                re_photo.save()
            
            # recalculate points for previous uploader
            if previous_uploader and previous_uploader['user']:
                uploader = Profile.objects.get(pk=previous_uploader['user'])
                uploader.update_rephoto_score()
            
            # recalculate points for new uploader
            profile.update_rephoto_score()
    
    return HttpResponse(json.dumps({'new_id': new_id}), mimetype="application/json")

def logout(request):
    from django.contrib.auth import logout
    logout(request)
    return redirect('/')
    #return HttpResponse(unicode(request.get_user()))

def thegame(request):
    ctx = {}
    city_select_form = CitySelectForm(request.GET)
    if city_select_form.is_valid():
        ctx['city'] = City.objects.get(pk=city_select_form.cleaned_data['city'])

    site = Site.objects.get_current()
    ctx['hostname'] = 'http://%s' % (site.domain, )
    ctx['title'] = _('Guess the location')
    
    filters = FilterSpecCollection(None, request.GET)
    filters.register(CityLookupFilterSpec, 'city')
    #filters.register(DateFieldFilterSpec, 'created')
    ctx['filters'] = filters
    
    return render_to_response('game.html', RequestContext(request, ctx))

def frontpage(request):
    try:
        example = random.choice(Photo.objects.filter(id__in=[2483, 2495, 2502, 3193, 3195, 3201, 3203, 3307], rephoto_of__isnull=False))
    except:
        example = random.choice(Photo.objects.filter(rephoto_of__isnull=False)[:8])
    example_source = Photo.objects.get(pk=example.rephoto_of.id)
    city_select_form = CitySelectForm(request.GET)
    
    if not city_select_form.is_valid():
        city_select_form = CitySelectForm()

    filters = FilterSpecCollection(None, request.GET)
    filters.register(CityLookupFilterSpec, 'city')
    
    return render_to_response('frontpage.html', RequestContext(request, {
        'city_select_form': city_select_form,
        'filters': filters,
        'example': example,
        'example_source': example_source,
    }))

def photo_large(request, photo_id):
    photo = get_object_or_404(Photo, id=photo_id)
    if (photo.cam_scale_factor and photo.rephoto_of):
        # if rephoto is taken with mobile then make it same width/height as source photo
        im = get_thumbnail(photo.rephoto_of.image, '1024x1024', upscale=False)
        im = get_thumbnail(photo.image, str(im.width) +'x'+ str(im.height), crop="center" )
    else:
        im = get_thumbnail(photo.image, '1024x1024', upscale=False)
    return redirect(im.url)

def photo_url(request, photo_id):
    photo = get_object_or_404(Photo, id=photo_id)
    if (photo.cam_scale_factor and photo.rephoto_of):
        # if rephoto is taken with mobile then make it same width/height as source photo
        im = get_thumbnail(photo.rephoto_of.image, '700x400')
        im = get_thumbnail(photo.image, str(im.width) +'x'+ str(im.height), crop="center" )
    else:
        im = get_thumbnail(photo.image, '700x400')
    return redirect(im.url)

def photo_thumb(request, photo_id):
    photo = get_object_or_404(Photo, id=photo_id)
    im = get_thumbnail(photo.image, '50x50', crop='center')
    return redirect(im.url)

def photo(request, photo_id):
    photo = get_object_or_404(Photo, id=photo_id)
    pseudo_slug = photo.get_pseudo_slug()
    # slug not needed if not enough data for slug or ajax request
    if pseudo_slug != "" and not request.is_ajax():
        return photoslug(request, photo.id, "")
    else:
        return photoslug(request, photo.id, pseudo_slug)

def photoslug(request, photo_id, pseudo_slug):
    photo_obj = get_object_or_404(Photo, id=photo_id)
    # redirect if slug in url doesn't match with our pseudo slug
    if photo_obj.get_pseudo_slug() != pseudo_slug:
        response = HttpResponse(content="", status=301) # HTTP 301 for google juice
        response["Location"] = photo_obj.get_absolute_url()
        return response

    # switch places if rephoto url
    rephoto = None
    if hasattr(photo_obj, 'rephoto_of') and photo_obj.rephoto_of is not None:
        rephoto = photo_obj
        photo_obj = photo_obj.rephoto_of

    site = Site.objects.get_current()
    template = ['', 'block_photoview.html', 'photoview.html'][request.is_ajax() and 1 or 2]
    return render_to_response(template, RequestContext(request, {
        'photo': photo_obj,
        'title': ' '.join(photo_obj.description.split(' ')[:5])[:50],
        'description': photo_obj.description,
        'rephoto': rephoto,
        'hostname': 'http://%s' % (site.domain, )
    }))

def photo_heatmap(request, photo_id):
    photo = get_object_or_404(Photo, id=photo_id)
    pseudo_slug = photo.get_pseudo_slug()
    # slug not needed if not enough data for slug or ajax request
    if pseudo_slug != "" and not request.is_ajax():
        return photoslug_heatmap(request, photo.id, "")
    else:
        return photoslug_heatmap(request, photo.id, pseudo_slug)


def photoslug_heatmap(request, photo_id, pseudo_slug):
    photo_obj = get_object_or_404(Photo, id=photo_id)
    # redirect if slug in url doesn't match with our pseudo slug
    if photo_obj.get_pseudo_slug() != pseudo_slug:
        response = HttpResponse(content="", status=301) # HTTP 301 for google juice
        response["Location"] = photo_obj.get_heatmap_url()
        return response

    # load heatmap data always from original photo
    if hasattr(photo_obj, 'rephoto_of') and photo_obj.rephoto_of is not None:
        photo_obj = photo_obj.rephoto_of

    data = get_next_photos_to_geotag.get_all_geotag_submits(photo_obj.id)
    return render_to_response('heatmap.html', RequestContext(request, {
        'json_data': json.dumps(data),
        'city': photo_obj.city,
        'title': ' '.join(photo_obj.description.split(' ')[:5])[:50] +' - '+ _("Heat map"),
        'description': photo_obj.description,
        'photo_lon': photo_obj.lon,
        'photo_lat': photo_obj.lat,
    }))

def heatmap(request):
    city_select_form = CitySelectForm(request.GET)
    city_id = city = None
    
    if city_select_form.is_valid():
        city_id = city_select_form.cleaned_data['city']
        city = City.objects.get(pk=city_id)
    else:
        city_select_form = CitySelectForm()
    
    data = get_next_photos_to_geotag.get_all_geotagged_photos(city_id)
    return render_to_response('heatmap.html', RequestContext(request, {
        'json_data': json.dumps(data),
        'city': city,
        'city_select_form': city_select_form,
        
    }))

def mapview(request):
    city = None
    get_params = request.GET.copy()
    city_id = get_params.get('city__pk', 0)
    
    if int(city_id) == 0:
        # backwards compatible with old city parameter
        city_id = get_params.get('city', 0)
        if int(city_id) > 0:
            get_params['city__pk'] = city_id

    if int(city_id) > 0:
        city = City.objects.get(pk=city_id)
    
    if city:
        title = city.name +' - '+ _('Browse photos on map')
    else:
        title = _('Browse photos on map')

    qs = Photo.objects.all()
    
    filters = FilterSpecCollection(qs, get_params)
    filters.register(CityLookupFilterSpec, 'city')
    #filters.register(DateFieldFilterSpec, 'created')
    #filters.register(SourceLookupFilterSpec, 'source')
    data = filters.get_filtered_qs().get_geotagged_photos_list()
    
    leaderboard = get_next_photos_to_geotag.get_rephoto_leaderboard(request.get_user().get_profile().pk)
    
    return render_to_response('mapview.html', RequestContext(request, {
        'json_data': json.dumps(data),
        'city': city,
        'title': title,
        'filters': filters,
        'leaderboard': leaderboard,
    }))

def get_leaderboard(request):
    return HttpResponse(json.dumps(
        get_next_photos_to_geotag.get_leaderboard(request.get_user().get_profile().pk)),
        mimetype="application/json")

def geotag_add(request):
    data = request.POST
    is_correct, current_score, total_score, leaderboard_update, location_is_unclear = get_next_photos_to_geotag.submit_guess(request.get_user().get_profile(), data['photo_id'], data.get('lon'), data.get('lat'), hint_used=data.get('hint_used'))
    return HttpResponse(json.dumps({
        'is_correct': is_correct,
        'current_score': current_score,
        'total_score': total_score,
        'leaderboard_update': leaderboard_update,
        'location_is_unclear': location_is_unclear,
    }), mimetype="application/json")

def leaderboard(request):
    # leaderboard with first position, one in front of you, your score and one after you
    leaderboard = get_next_photos_to_geotag.get_leaderboard(request.get_user().get_profile().pk)
    template = ['', 'block_leaderboard.html', 'leaderboard.html'][request.is_ajax() and 1 or 2]
    return render_to_response(template, RequestContext(request, {
        'leaderboard': leaderboard,
        'title': _('Leaderboard'),
    }))

def top50(request):
    # leaderboard with top 50 scores
    leaderboard = get_next_photos_to_geotag.get_leaderboard50(request.get_user().get_profile().pk)
    template = ['', 'block_leaderboard.html', 'leaderboard.html'][request.is_ajax() and 1 or 2]
    return render_to_response(template, RequestContext(request, {
        'leaderboard': leaderboard,
        'title': _('Leaderboard'),
    }))
    
def rephoto_top50(request):
    # leaderboard with top 50 scores
    leaderboard = get_next_photos_to_geotag.get_rephoto_leaderboard50(request.get_user().get_profile().pk)
    template = ['', 'block_leaderboard.html', 'leaderboard.html'][request.is_ajax() and 1 or 2]
    return render_to_response(template, RequestContext(request, {
        'leaderboard': leaderboard,
        'title': _('Leaderboard'),
    }))

def fetch_stream(request):
    qs = Photo.objects.all()
    filters = FilterSpecCollection(qs, request.GET)
    filters.register(DateFieldFilterSpec, 'created')
    filters.register(CityLookupFilterSpec, 'city')
    filters.register(SourceLookupFilterSpec, 'source')
    data = filters.get_filtered_qs().get_next_photos_to_geotag(request.get_user().get_profile(), 4)
    return HttpResponse(json.dumps(data), mimetype="application/json")
