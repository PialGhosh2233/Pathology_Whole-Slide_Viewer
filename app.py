import io
import os
import threading

import gradio as gr
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from openslide import OpenSlide, OpenSlideError
from openslide.deepzoom import DeepZoomGenerator

TILE_SIZE = 254
TILE_OVERLAP = 1
TILE_FORMAT = "jpeg"
TILE_QUALITY = 85

_lock = threading.Lock()
_state: dict = {"slide": None, "dz": None}


def set_slide(path: str):
    slide = OpenSlide(path)
    dz = DeepZoomGenerator(
        slide,
        tile_size=TILE_SIZE,
        overlap=TILE_OVERLAP,
        limit_bounds=True,
    )
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
    import sys
    import time

    if sys.platform != "win32":
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path, "rb") as f:
                f.read(8)
            return True
        except PermissionError:
            time.sleep(2)
    return False


_VIEWER_PAGE = """<!DOCTYPE html>
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
      tileSources: "dzi.dzi",
      showNavigator: true,
      navigatorPosition: "BOTTOM_RIGHT",
      animationTime: 0.3,
      blendTime: 0.1,
      constrainDuringPan: true,
      maxZoomPixelRatio: 4,
      minZoomImageRatio: 0.8,
      gestureSettingsMouse: { scrollToZoom: true, clickToZoom: false, dblClickToZoom: true },
      gestureSettingsTouch: { pinchToZoom: true }
    });
  </script>
</body>
</html>"""


EMPTY_VIEWER = """
<div style="height:620px;display:flex;align-items:center;justify-content:center;
            color:#888;background:#111;border-radius:8px;font-size:16px;">
  Upload a slide to start viewing
</div>
"""

OSD_VIEWER = """<iframe
  src="viewer"
  style="width:100%;height:620px;border:none;border-radius:8px;background:#000;"
></iframe>"""


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
        f"| Dimensions (L0) | {full_w:,} x {full_h:,} px |",
        f"| Zoom levels | {slide.level_count} |",
        f"| Objective power | {props.get('openslide.objective-power', 'N/A')}x |",
        f"| um/px (X) | {props.get('openslide.mpp-x', 'N/A')} |",
        f"| um/px (Y) | {props.get('openslide.mpp-y', 'N/A')} |",
        "",
        "**All levels:**",
        "| Level | W x H | Downsample |",
        "|---|---|---|",
    ]
    for lvl in range(slide.level_count):
        w, h = slide.level_dimensions[lvl]
        ds = slide.level_downsamples[lvl]
        info_lines.append(f"| {lvl} | {w:,} x {h:,} | {ds:.2f}x |")

    info_lines += ["", "**Raw properties:**", "```"]
    for key, value in sorted(props.items()):
        info_lines.append(f"{key} = {value}")
    info_lines.append("```")

    assoc_images = [
        (image.convert("RGB"), name)
        for name, image in slide.associated_images.items()
    ]

    return thumb, "\n".join(info_lines), assoc_images, OSD_VIEWER


def build_app() -> FastAPI:
    with gr.Blocks(title="Pathology Whole-Slide Viewer") as demo:
        supported_formats_md = """
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

        gr.Markdown(
            "# Pathology Whole-Slide Viewer\n"
            "Upload a whole-slide image - **scroll to zoom, drag to pan, double-click to zoom in**."
        )

        with gr.Row():
            file_upload = gr.File(
                label="Upload Slide (.svs, .tif, .tiff, .ndpi, .mrxs, ...)",
                file_types=[
                    ".svs",
                    ".tif",
                    ".tiff",
                    ".ndpi",
                    ".mrxs",
                    ".scn",
                    ".bif",
                    ".vms",
                    ".vmu",
                    ".czi",
                    ".dcm",
                    ".svslide",
                ],
                type="filepath",
            )

        with gr.Row():
            with gr.Column(scale=1):
                thumbnail_out = gr.Image(
                    label="Slide Overview",
                    interactive=False,
                    height=260,
                )
                gr.Markdown(supported_formats_md)
            with gr.Column(scale=3):
                viewer_html = gr.HTML(value=EMPTY_VIEWER)

        with gr.Tabs():
            with gr.Tab("Slide Info"):
                info_out = gr.Markdown("*Upload a slide to see metadata.*")
            with gr.Tab("Associated Images"):
                assoc_gallery = gr.Gallery(
                    label="Associated Images (label, macro, ...)",
                    columns=3,
                    height=300,
                )

        file_upload.upload(
            fn=load_slide,
            inputs=[file_upload],
            outputs=[thumbnail_out, info_out, assoc_gallery, viewer_html],
        )

    app = FastAPI()

    @app.get("/viewer")
    async def viewer() -> HTMLResponse:
        return HTMLResponse(_VIEWER_PAGE)

    @app.get("/dzi.dzi")
    async def dzi() -> Response:
        dz = get_dz()
        if dz is None:
            raise HTTPException(status_code=404, detail="No slide loaded")
        return Response(content=dz.get_dzi(TILE_FORMAT), media_type="application/xml")

    @app.get("/dzi_files/{level:int}/{tile_name}")
    async def dzi_tile(level: int, tile_name: str) -> Response:
        dz = get_dz()
        if dz is None:
            raise HTTPException(status_code=404, detail="No slide loaded")

        stem, ext = os.path.splitext(tile_name)
        if ext.lower() != f".{TILE_FORMAT}":
            raise HTTPException(status_code=404, detail="Unsupported tile format")

        try:
            col_str, row_str = stem.split("_", maxsplit=1)
            tile = dz.get_tile(level, (int(col_str), int(row_str)))
        except (OpenSlideError, ValueError):
            raise HTTPException(status_code=404, detail="Tile not found") from None

        buf = io.BytesIO()
        tile.convert("RGB").save(buf, format="JPEG", quality=TILE_QUALITY)
        return Response(content=buf.getvalue(), media_type="image/jpeg")

    return gr.mount_gradio_app(app, demo, path="/", ssr_mode=False)


app = build_app()


if __name__ == "__main__":
    host = "0.0.0.0" if os.getenv("SPACE_ID") else "127.0.0.1"
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host=host, port=port)
