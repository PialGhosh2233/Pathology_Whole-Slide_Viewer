import os

import gradio as gr
from openslide import OpenSlide, OpenSlideError
from openslide.deepzoom import DeepZoomGenerator
from socketserver import ThreadingMixIn, TCPServer
from http.server import BaseHTTPRequestHandler
import threading
import io

TILE_PORT = 8001
TILE_SIZE = 254
TILE_OVERLAP = 1
TILE_FORMAT = "jpeg"
TILE_QUALITY = 85

# ── Global slide state ──────────────────────────────────────────────────────

_lock = threading.Lock()
_state: dict = {"slide": None, "dz": None}


def set_slide(path: str):
    slide = OpenSlide(path)
    dz = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP, limit_bounds=True)
    with _lock:
        old = _state["slide"]
        _state["slide"] = slide
        _state["dz"] = dz
    if old and old is not slide:
        try:
            old.close()
        except Exception:
            pass
    return slide, dz


def get_dz():
    with _lock:
        return _state["dz"]


def wait_readable(path: str, timeout: int = 60) -> bool:
      import sys, time
      if sys.platform != "win32":
          return True  # no AV file locking on Linux/Mac
      deadline = time.monotonic() + timeout
      while time.monotonic() < deadline:
          try:
              with open(path, "rb") as f:
                  f.read(8)
              return True
          except PermissionError:
              time.sleep(2)
      return False



# ── Tile server — pure Python stdlib, no asyncio ────────────────────────────
# Using stdlib http.server avoids asyncio-in-thread issues that interfered
# with OpenSlide's native DLL loading on Windows.

class TileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.split("?")[0]  # strip query string

        if parts == "/viewer":
            self.serve_viewer()
        elif parts == "/dzi.dzi":
            self.serve_dzi()
        elif parts.startswith("/dzi_files/"):
            self.serve_tile(parts)
        else:
            self.send_response(404)
            self.end_headers()

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")

    def serve_viewer(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_VIEWER_PAGE)))
        self.end_headers()
        self.wfile.write(_VIEWER_PAGE)

    def serve_dzi(self):
        dz = get_dz()
        if dz is None:
            self.send_response(404)
            self.end_headers()
            return
        data = dz.get_dzi(TILE_FORMAT).encode()
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_tile(self, path: str):
        # /dzi_files/{level}/{col}_{row}.jpeg
        try:
            segments = path.strip("/").split("/")
            level = int(segments[1])
            col, row = map(int, segments[2].split(".")[0].split("_"))
        except (IndexError, ValueError):
            self.send_response(400)
            self.end_headers()
            return

        dz = get_dz()
        if dz is None:
            self.send_response(404)
            self.end_headers()
            return

        try:
            tile = dz.get_tile(level, (col, row))
            buf = io.BytesIO()
            tile.convert("RGB").save(buf, format="JPEG", quality=TILE_QUALITY)
            data = buf.getvalue()
            self.send_response(200)
            self.cors()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress per-request console noise


class ThreadedServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# HTML page served by the tile server — scripts execute here because it's a
# real document (not innerHTML-injected), and tile URLs are same-origin (port 8001).
_VIEWER_PAGE = b"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #000; overflow: hidden; }
    #osd { width: 100vw; height: 100vh; }
  </style>
</head>
<body>
  <div id="osd"></div>
  <script src="https://cdn.jsdelivr.net/npm/openseadragon@3.1/build/openseadragon/openseadragon.min.js"></script>
  <script>
    OpenSeadragon({
      id: "osd",
      prefixUrl: "https://cdn.jsdelivr.net/npm/openseadragon@3.1/build/openseadragon/images/",
      tileSources: "/dzi.dzi",
      showNavigator: true,
      navigatorPosition: "BOTTOM_RIGHT",
      animationTime: 0.3,
      blendTime: 0.1,
      constrainDuringPan: true,
      maxZoomPixelRatio: 4,
      minZoomImageRatio: 0.8,
      gestureSettingsMouse: { scrollToZoom: true, clickToZoom: false, dblClickToZoom: true },
      gestureSettingsTouch:  { pinchToZoom: true },
    });
  </script>
</body>
</html>"""


def start_tile_server():
    server = ThreadedServer(("127.0.0.1", TILE_PORT), TileHandler)
    server.serve_forever()


# ── Viewer HTML ─────────────────────────────────────────────────────────────

EMPTY_VIEWER = """
<div style="height:620px;display:flex;align-items:center;justify-content:center;
            color:#888;background:#111;border-radius:8px;font-size:16px;">
  Upload a slide to start viewing
</div>
"""

# Embed the viewer as an iframe so its scripts actually execute.
# Tiles use relative URLs (/dzi.dzi) so they're same-origin — no CORS needed.
OSD_VIEWER = f"""<iframe
  src="http://127.0.0.1:{TILE_PORT}/viewer"
  style="width:100%;height:620px;border:none;border-radius:8px;background:#000;"
></iframe>"""


# ── Gradio functions ────────────────────────────────────────────────────────

def load_slide(file_obj):
    if file_obj is None:
        return None, "*Upload a slide to see metadata.*", [], EMPTY_VIEWER

    path = file_obj if isinstance(file_obj, str) else file_obj.name

    if not os.path.exists(path):
        return None, f"**Error:** uploaded file not found at `{path}`", [], EMPTY_VIEWER

    if not wait_readable(path):
        return (
            None,
            "**Error:** the file is still locked by antivirus/Windows Defender after 60 s. "
            "Try adding the Gradio temp folder to your antivirus exclusion list, "
            "or use a direct file path instead of uploading.",
            [],
            EMPTY_VIEWER,
        )

    try:
        slide, _ = set_slide(path)
    except OpenSlideError as e:
        return None, f"**Error opening slide:** {e}", [], EMPTY_VIEWER
    except Exception as e:
        return None, f"**Unexpected error:** {type(e).__name__}: {e}", [], EMPTY_VIEWER

    thumb = slide.get_thumbnail((512, 512)).convert("RGB")

    props = slide.properties
    full_w, full_h = slide.dimensions

    info_lines = [
        "| Property | Value |",
        "|---|---|",
        f"| Vendor | {props.get('openslide.vendor', 'N/A')} |",
        f"| Dimensions (L0) | {full_w:,} × {full_h:,} px |",
        f"| Zoom levels | {slide.level_count} |",
        f"| Objective power | {props.get('openslide.objective-power', 'N/A')}× |",
        f"| µm/px (X) | {props.get('openslide.mpp-x', 'N/A')} |",
        f"| µm/px (Y) | {props.get('openslide.mpp-y', 'N/A')} |",
        "",
        "**All levels:**",
        "| Level | W × H | Downsample |",
        "|---|---|---|",
    ]
    for lvl in range(slide.level_count):
        w, h = slide.level_dimensions[lvl]
        ds = slide.level_downsamples[lvl]
        info_lines.append(f"| {lvl} | {w:,} × {h:,} | {ds:.2f}× |")
    info_lines += ["", "**Raw properties:**", "```"]
    for k, v in sorted(props.items()):
        info_lines.append(f"{k} = {v}")
    info_lines.append("```")

    assoc_images = [(img.convert("RGB"), name) for name, img in slide.associated_images.items()]

    return thumb, "\n".join(info_lines), assoc_images, OSD_VIEWER


# ── Gradio UI ───────────────────────────────────────────────────────────────
SUPPORTED_FORMATS_MD = """
**Pathology Whole-Slide Viewer** can read virtual slides in several formats:

- Aperio (`.svs`, `.tif`)
- DICOM (`.dcm`)
- Hamamatsu (`.ndpi`, `.vms`, `.vmu`)
- Leica (`.scn`)
- MIRAX (`.mrxs`)
- Philips (`.tiff`)
- Sakura (`.svslide`)
- Trestle (`.tif`)
- Ventana (`.bif`, `.tif`)
- Zeiss (`.czi`)
- Generic tiled TIFF (`.tif`)
"""

with gr.Blocks(title="Pathology Whole-Slide Viewer") as demo:
    gr.Markdown(
        "# Pathology Whole-Slide Viewer\n"
        "Upload a whole-slide image — **scroll to zoom · drag to pan · double-click to zoom in**."
    )

    with gr.Row():
        file_upload = gr.File(
            label="Upload Slide (.svs, .tif, .tiff, .ndpi, .mrxs, …)",
            file_types=[".svs", ".tif", ".tiff", ".ndpi", ".mrxs", ".scn", ".bif", ".vms", ".vmu"],
            type="filepath",
        )

    with gr.Row():
        with gr.Column(scale=1):
            thumbnail_out = gr.Image(label="Slide Overview", interactive=False, height=260)
            gr.Markdown(SUPPORTED_FORMATS_MD)
        with gr.Column(scale=3):
            viewer_html = gr.HTML(value=EMPTY_VIEWER)

    with gr.Tabs():
        with gr.Tab("Slide Info"):
            info_out = gr.Markdown("*Upload a slide to see metadata.*")
        with gr.Tab("Associated Images"):
            assoc_gallery = gr.Gallery(label="Associated Images (label, macro, …)", columns=3, height=300)

    file_upload.upload(
        fn=load_slide,
        inputs=[file_upload],
        outputs=[thumbnail_out, info_out, assoc_gallery, viewer_html],
    )


if __name__ == "__main__":
    threading.Thread(target=start_tile_server, daemon=True).start()
    demo.launch(max_file_size=None)
