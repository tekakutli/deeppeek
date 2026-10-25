let _els = null;

function _get() {
  if (_els) return _els;
  _els = {
    imageInfo:    document.getElementById("imageInfo"),
    maskCount:    document.getElementById("maskCount"),
    inpaintFullCanvas: document.getElementById("inpaintFullCanvas"),
    inpaintRectHint:   document.getElementById("inpaintRectHint"),
  };
  return _els;
}

export { _get };
