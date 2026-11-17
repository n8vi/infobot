import base64
import os
from pathlib import Path
import tempfile
import unittest
from maps import MapIndex, group_key

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

class MapTests(unittest.TestCase):
    def test_emwin_product_group(self):
        a = Path('EMWIN/NWS/A_TEST_C_KWIN_20261107120000_123-2-RADARUS.gif')
        b = Path('EMWIN/NWS/A_TEST_C_KWIN_20261107130000_124-2-RADARUS.gif')
        self.assertEqual(group_key(a), group_key(b))

    def test_goes_product_group(self):
        a = Path('IMAGES/GOES-19/Full Disk/20261107T120000Z/G19_13_20261107T120000Z.png')
        b = Path('IMAGES/GOES-19/Full Disk/20261107T130000Z/G19_13_20261107T130000Z.png')
        self.assertEqual(group_key(a), group_key(b))
        self.assertNotEqual(group_key(a), group_key(Path(str(b).replace('G19_13', 'G19_7'))))

    def test_latest_filter_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for timestamp in ('20261107120000', '20261107130000'):
                p = root/'EMWIN'/'NWS'/f'A_TEST_C_KWIN_{timestamp}_123-2-RADARUS.png'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(PNG)
                os.utime(p,(100,100))
            bad = root/'EMWIN'/'NWS'/'not_an_image.png'
            bad.write_text('<script>no</script>')
            os.utime(bad,(100,100))
            i=MapIndex(root);i.scan()
            data=i.catalog({})
            self.assertEqual(len(data['maps']),1)
            item=data['maps'][0]
            self.assertIn('20261107130000',item['filename'])
            with i.open_image(item['id'])[0] as f: self.assertEqual(f.read(), PNG)
            self.assertEqual(i.catalog({'source':['IMAGES']})['maps'],[])
            self.assertEqual(i.catalog({'q':['radar']})['maps'][0]['id'],item['id'])
            with self.assertRaises(FileNotFoundError):i.open_image('../../etc/passwd')

    def test_symlink_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'EMWIN').mkdir()
            (root/'outside.png').write_bytes(PNG)
            (root/'EMWIN'/'image.png').symlink_to(root/'outside.png')
            i=MapIndex(root);i.scan()
            self.assertEqual(i.catalog({})['maps'],[])

    def test_text_directory_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'EMWIN'/'Text'/'wrong.png';p.parent.mkdir(parents=True);p.write_bytes(PNG)
            os.utime(p,(1,1))
            i=MapIndex(d);i.scan()
            self.assertEqual(i.catalog({})['files_examined'],0)
