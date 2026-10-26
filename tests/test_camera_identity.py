import unittest
from unittest.mock import patch
from types import SimpleNamespace
import cv2
from face_service.camera import Camera

def device(index, path, name='NearStream V02AF'):
 return SimpleNamespace(index=index,path=path,name=name)
class CameraIdentityTests(unittest.TestCase):
 def test_reordered_camera_uses_its_new_index_in_each_backend(self):
  cam=Camera(device_id='USB#chosen')
  with patch('cv2_enumerate_cameras.enumerate_cameras',side_effect=[[device(0,'USB#other'),device(2,'usb#chosen#{dshow}')],[device(1,'USB#CHOSEN#{msmf}')]]):
   self.assertEqual(cam._candidates(),[(2,cv2.CAP_DSHOW),(1,cv2.CAP_MSMF)])
 def test_missing_camera_never_opens_other_camera(self):
  with patch('cv2_enumerate_cameras.enumerate_cameras',return_value=[device(0,'USB#other')]), patch('face_service.camera.cv2.VideoCapture') as capture:
   with self.assertRaisesRegex(RuntimeError,'disconnected'):
    Camera(device_id='USB#chosen').open()
   capture.assert_not_called()
 def test_duplicate_identity_is_rejected(self):
  with patch('cv2_enumerate_cameras.enumerate_cameras',return_value=[device(0,'USB#chosen'),device(1,'USB#chosen')]):
   with self.assertRaisesRegex(RuntimeError,'ambiguous'):
    Camera(device_id='USB#chosen')._candidates()
 def test_config_is_shared(self):
  cfg=SimpleNamespace(camera_index=3,camera_warmup_frames=4,camera_device_id='USB#chosen')
  cam=Camera.from_config(cfg)
  self.assertEqual((cam.index,cam.warmup_frames,cam.device_id),(3,4,'USB#chosen'))
if __name__=='__main__': unittest.main()
