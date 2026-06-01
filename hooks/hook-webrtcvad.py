"""
Custom hook for webrtcvad to override the system hook.

The system hook fails because it tries to copy_metadata('webrtcvad'),
but webrtcvad-wheels provides the module under a different distribution name.
This hook manually collects the binary without requiring metadata.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

# Collect the webrtcvad binary (the .pyd file)
binaries = collect_dynamic_libs('webrtcvad')

# No datas/metadata needed - just the compiled extension
datas = []
hiddenimports = []
