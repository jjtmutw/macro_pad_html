import sys
import types


module_name = "PyInstaller.utils.hooks.tcl_tk"

if module_name not in sys.modules:
    tcl_tk = types.ModuleType(module_name)
    tcl_tk.TCL_ROOTNAME = "tcl"
    tcl_tk.TK_ROOTNAME = "tk"

    def _not_available(*args, **kwargs):
        raise RuntimeError("Tcl/Tk is not available in this Python installation.")

    tcl_tk.collect_tcl_tk_files = lambda *args, **kwargs: []
    tcl_tk.find_tcl_tk_shared_libs = _not_available
    sys.modules[module_name] = tcl_tk
