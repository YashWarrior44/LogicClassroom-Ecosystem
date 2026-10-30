// =============================================================================
// module.cpp — PYBIND11_MODULE entry point for the logicboard_engine extension.
//
// Each bind_*.cpp file defines a registration function that adds its
// classes/functions to the module.  This file calls them all.
// =============================================================================

#include <pybind11/pybind11.h>

namespace py = pybind11;

// Forward declarations of registration functions defined in other TUs.
void bind_scene(py::module_& m);
void bind_commands(py::module_& m);

PYBIND11_MODULE(logicboard_engine, m) {
    m.doc() = "LogicBoard C++ engine — scene graph, shapes, and command stack";

    bind_scene(m);
    bind_commands(m);
}