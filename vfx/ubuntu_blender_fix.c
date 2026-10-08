/* Python 3.12 made PyImport_AppendInittab() fatal after Py_Initialize().
   Blender 4.0 (Ubuntu build) calls it late to register the Mantaflow module.
   This shim runs the init function immediately and registers the module instead. */
#include <Python.h>
int PyImport_AppendInittab(const char *name, PyObject *(*initfunc)(void)) {
    PyObject *mod = initfunc();
    if (!mod) return -1;
    PyObject *dict = PyImport_GetModuleDict();
    int rc = PyDict_SetItemString(dict, name, mod);
    Py_DECREF(mod);
    return rc;
}
