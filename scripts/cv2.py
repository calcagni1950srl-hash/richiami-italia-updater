import importlib
import os
import subprocess
import sys

# Compatibilità temporanea con i vecchi tentativi del workflow immagini,
# che installavano Pillow ma non OpenCV. I workflow correnti installano già
# queste dipendenze e questo file verrà rimosso dopo il test.
subprocess.check_call(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "numpy",
        "opencv-python-headless",
    ]
)

module_name = __name__
script_dir = os.path.abspath(os.path.dirname(__file__))
original_path = list(sys.path)

sys.modules.pop(module_name, None)
sys.path = [
    entry
    for entry in sys.path
    if os.path.abspath(entry or os.getcwd()) != script_dir
]

try:
    real_cv2 = importlib.import_module(module_name)
finally:
    sys.path = original_path

sys.modules[module_name] = real_cv2
globals().update(real_cv2.__dict__)
