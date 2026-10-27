# Third-party notices

Face Unlock for Windows is MIT-licensed (see [LICENSE](LICENSE)). It builds on
the projects below, each under its own licence. The installer contains the
Python packages and the two bundled model files listed here.

| Component | Used for | Licence |
|---|---|---|
| [windows-face-unlock](https://github.com/caochitam/windows-face-unlock) by Cao Chí Tâm | The original project this one grew from | MIT |
| [DeepFace](https://github.com/serengil/deepface) | Face detection, alignment and recognition pipeline | MIT |
| [OpenCV](https://opencv.org/) and the [YuNet](https://github.com/opencv/opencv_zoo) face detector (`models/face_detection_yunet_2023mar.onnx`) | Camera access, image processing, face detection | Apache-2.0 / MIT |
| [MiniFASNet](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing) weights | Photo protection (liveness) | Apache-2.0 |
| [TensorFlow](https://www.tensorflow.org/) and tf-keras | Running the recognition model | Apache-2.0 |
| [PyTorch](https://pytorch.org/) and torchvision | Running the liveness model | BSD-3-Clause |
| [Qt for Python (PySide6)](https://doc.qt.io/qtforpython/) | The desktop app | LGPL-3.0 (dynamically linked; you may replace the Qt libraries in the install folder) |
| [NumPy](https://numpy.org/) | Numerics | BSD-3-Clause |
| [pywin32](https://github.com/mhammond/pywin32) | Windows APIs | PSF |
| [psutil](https://github.com/giampaolo/psutil) | Process information | BSD-3-Clause |
| [tomli-w](https://github.com/hukkin/tomli-w) | Writing settings | MIT |
| [cv2-enumerate-cameras](https://github.com/chinaheyu/cv2_enumerate_cameras) | Listing cameras | MIT |

**Not included:** the ArcFace recognition weights. The app downloads them
once, on first start, from DeepFace's own release page (about 130 MB). They
were trained on datasets generally licensed for non-commercial research;
check their terms before any commercial use.
