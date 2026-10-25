let _els = null;

function _get() {
  if (_els) return _els;
  _els = {
    imageInfo:    document.getElementById("imageInfo"),
    maskCount:    document.getElementById("maskCount"),
  };
  return _els;
}

export { _get };
