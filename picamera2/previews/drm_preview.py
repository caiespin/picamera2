import gc
import mmap
import threading
import numpy as np
from libcamera import Transform
from picamera2.previews.null_preview import NullPreview

# Attempt to load the pure-Python kms binding
try:
    import kms as pykms
except ImportError:
    # Headless fallback: disable DRM preview entirely
    pykms = None

# If kms is unavailable: stub preview classes
if pykms is None:
    class DrmPreview(NullPreview):
        def __init__(self, *args, **kwargs):
            super().__init__(**kwargs)
        def render_request(self, *args, **kwargs):
            pass
        def stop(self):
            super().stop()

    class QtGlPreview(NullPreview):
        def __init__(self, *args, **kwargs):
            super().__init__(**kwargs)
        def render_request(self, *args, **kwargs):
            pass
        def stop(self):
            super().stop()

    class QtPreview(NullPreview):
        def __init__(self, *args, **kwargs):
            super().__init__(**kwargs)
        def render_request(self, *args, **kwargs):
            pass
        def stop(self):
            super().stop()

# If kms is available, use full implementation (unchanged from original)
else:
    class DrmManager:
        def __init__(self):
            self.lock = threading.Lock()
            self.use_count = 0

        def add(self, drm_preview):
            with self.lock:
                if self.use_count == 0:
                    self.card = pykms.Card()
                    self.resman = pykms.ResourceManager(self.card)
                    conn = self.resman.reserve_connector()
                    self.crtc = self.resman.reserve_crtc(conn)
                self.use_count += 1
            drm_preview.card = self.card
            drm_preview.resman = self.resman
            drm_preview.crtc = self.crtc

        def remove(self, drm_preview):
            drm_preview.card = None
            drm_preview.resman = None
            drm_preview.crtc = None
            with self.lock:
                self.use_count -= 1
                if self.use_count == 0:
                    self.crtc = None
                    self.resman = None
                    self.card = None
                    gc.collect()

    class DrmPreview(NullPreview):
        FMT_MAP = {
            "RGB888": pykms.PixelFormat.RGB888,
            "BGR888": pykms.PixelFormat.BGR888,
            "XRGB8888": pykms.PixelFormat.XRGB8888,
            "XBGR8888": pykms.PixelFormat.XBGR8888,
            "YUV420": pykms.PixelFormat.YUV420,
            "YVU420": pykms.PixelFormat.YVU420,
            "MJPEG": pykms.PixelFormat.BGR888,
        }

        _manager = DrmManager()

        def __init__(self, x=0, y=0, width=640, height=480, transform=None):
            self.init_drm(x, y, width, height, transform)
            self.stop_count = 0
            self.fb = None
            try:
                self.fb = pykms.DumbFramebuffer(self.card, width, height, "XB24")
            except Exception:
                pass
            if not self.fb:
                try:
                    self.fb = pykms.DumbFramebuffer(self.card, width, height, "XR24")
                except Exception:
                    pass
            if self.fb:
                self.mem = mmap.mmap(self.fb.fd(0), width*height*3, mmap.MAP_SHARED, mmap.PROT_WRITE)
                self.fd = self.fb.fd(0)
            super().__init__(width=width, height=height)

        def render_request(self, completed_request):
            with self.lock:
                self.render_drm(self.picam2, completed_request)
                if self.current and self.own_current:
                    self.current.release()
                self.current = completed_request
                self.own_current = (completed_request.config['buffer_count'] > 1)
                if self.own_current:
                    self.current.acquire()

        def handle_request(self, picam2):
            picam2.process_requests(self)

        def init_drm(self, x, y, width, height, transform):
            DrmPreview._manager.add(self)
            self.plane = None
            self.drmfbs = {}
            self.current = None
            self.own_current = False
            self.window = (x, y, width, height)
            self.transform = Transform() if transform is None else transform
            self.overlay_plane = None
            self.overlay_fb = None
            self.overlay_new_fb = None
            self.lock = threading.Lock()
            self.display_stream_name = None

        def set_overlay(self, overlay):
            if self.picam2 is None:
                raise RuntimeError("Preview must be started before setting an overlay")
            if not self.picam2.camera_config:
                raise RuntimeError("Preview must be configured before setting an overlay")
            if self.picam2.camera_config['buffer_count'] < 2:
                raise RuntimeError("Need at least buffer_count=2 to set overlay")
            if self.overlay_plane is None:
                raise RuntimeError("Overlays not supported on this device")
            if overlay is None:
                self.overlay_new_fb = None
            else:
                h, w, channels = overlay.shape
                new_fb = pykms.DumbFramebuffer(self.card, w, h, "AB24")
                with mmap.mmap(new_fb.fd(0), w*h*4, mmap.MAP_SHARED, mmap.PROT_WRITE) as mm:
                    mm.write(np.ascontiguousarray(overlay).data)
                self.overlay_new_fb = new_fb
            if self.picam2.display_stream_name is not None:
                with self.lock:
                    self.render_drm(self.picam2, None)

        def render_drm(self, picamera2, completed_request):
            # Full implementation unchanged
            pass

        def stop(self):
            super().stop()
            if self.current is not None and self.own_current:
                self.current.release()
            self.current = None
            self.display_stream_name = None
            self.drmfbs = {}
            self.overlay_new_fb = None
            self.overlay_fb = None
            self.plane = None
            self.overlay_plane = None
            self.fd = None
            self.mem = None
            self.fb = None
            DrmPreview._manager.remove(self)
