import copy
from types import SimpleNamespace
import unittest

from core import Config, DTX_COUNTIES, Engine, PINNEBOG_COORDINATES, request_location
from test_core import NOW, WX

SENDER='11'*32
LOCAL='aabb'+'00'*30
REMOTE='ccdd'+'00'*30


class DMLocationTests(unittest.TestCase):
    def contacts(self):
        _name,lat,lon=DTX_COUNTIES['lapeer']
        return {
            SENDER:{'type':1,'out_path_len':2,'out_path_hash_mode':1,'out_path':'aabbccdd'},
            LOCAL:{'type':2,'adv_name':'Near bot','adv_lat':43.96,'adv_lon':-83.10},
            REMOTE:{'type':2,'adv_name':'Near sender','adv_lat':lat,'adv_lon':lon},
        }

    def payload(self):
        return {'_dm_sender':SENDER,'path_len':255,'path_hash_mode':-1}

    def test_dm_uses_last_outbound_hop_even_for_direct_delivery(self):
        for route in ({},{'route_type':2}):
            location,error=request_location(dict(self.payload(),**route),self.contacts())
            self.assertIsNone(error)
            self.assertEqual(location['county'],'lapeer')
            self.assertEqual(location['name'],'Near sender')
            self.assertEqual(location['basis'],'dm_return_route')

    def test_dm_and_equivalent_channel_route_choose_same_county(self):
        contacts=self.contacts()
        dm,_=request_location(self.payload(),contacts)
        channel,_=request_location({'path':'ccddaabb','path_len':2,'path_hash_mode':1,'route_type':1},contacts)
        self.assertEqual(dm['county'],channel['county'])

    def test_weather_uses_remote_county_and_labels_estimate(self):
        data=copy.deepcopy(WX)
        data['counties']['lapeer']=data['counties'].pop('huron')
        source=SimpleNamespace(get=lambda *args,**kwargs:data)
        reply=Engine(Config(),source).execute('wx',[],self.payload(),self.contacts(),NOW)
        self.assertIn('WX near Near sender (DM route estimate)',reply)
        self.assertIn('Lapeer WX',reply)
        self.assertNotIn('Huron',reply)

    def test_unknown_or_malformed_route_never_silently_defaults(self):
        for fields in ({'out_path_len':-1,'out_path':''},
                       {'out_path_len':255,'out_path':''},
                       {'out_path_hash_mode':None},
                       {'out_path':'aabbcc'},
                       {'out_path':'zzzzzzzz'}):
            contacts=self.contacts();contacts[SENDER].update(fields)
            location,error=request_location(self.payload(),contacts)
            self.assertIsNone(location)
            self.assertIn('Location unavailable',error)
        self.assertIsNone(request_location(self.payload(),{})[0])

    def test_unknown_ambiguous_or_coordinate_free_repeater_is_unavailable(self):
        for change in ('unknown','ambiguous','coordinates'):
            contacts=self.contacts()
            if change=='unknown':del contacts[REMOTE]
            elif change=='ambiguous':contacts['ccdd'+'22'*30]=dict(contacts[REMOTE])
            else:contacts[REMOTE].pop('adv_lat')
            location,error=request_location(self.payload(),contacts)
            self.assertIsNone(location)
            self.assertIn('DM sender-side',error)

    def test_one_and_three_byte_hop_widths_preserve_zero_bytes(self):
        for mode,path,key in ((0,'aacc','cc'+'00'*31),
                              (2,'aabb00ccdd00','ccdd00'+'00'*29)):
            contacts=self.contacts()
            contacts[SENDER].update(out_path_hash_mode=mode,out_path=path)
            contacts[key]=contacts.pop(REMOTE)
            location,error=request_location(self.payload(),contacts)
            self.assertIsNone(error)
            self.assertEqual(location['county'],'lapeer')

    def test_confirmed_zero_hop_keeps_local_fallback(self):
        contacts=self.contacts();contacts[SENDER].update(out_path_len=0,out_path='')
        location,error=request_location(self.payload(),contacts)
        self.assertIsNone(error)
        self.assertEqual((location['lat'],location['lon']),PINNEBOG_COORDINATES)

    def test_explicit_county_works_with_unknown_dm_route(self):
        data=copy.deepcopy(WX)
        source=SimpleNamespace(get=lambda *args,**kwargs:data)
        reply=Engine(Config(),source).execute('wx',['huron'],self.payload(),{},NOW)
        self.assertIn('Huron WX',reply)
        self.assertNotIn('route estimate',reply)
