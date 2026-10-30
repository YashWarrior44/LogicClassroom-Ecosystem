// =============================================================================
// bind_commands.cpp — Expose the command/undo-redo system to Python.
//
// Binds: Command (abstract), CommandStack, AddObjectCmd, DeleteObjectCmd,
//        MoveObjectCmd, TransformObjectCmd, StrokeDrawCmd, CompoundCmd.
// =============================================================================
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>

#include <command/Command.h>
#include <command/CommandStack.h>
#include <command/AddObjectCmd.h>
#include <command/DeleteObjectCmd.h>
#include <command/MoveObjectCmd.h>
#include <command/TransformObjectCmd.h>
#include <command/StrokeDrawCmd.h>
#include <command/CompoundCmd.h>
#include <command/SetColorCmd.h>
#include <command/SetBackgroundCmd.h>
#include <command/SetSlideCmd.h>

#include <scene/Scene.h>
#include <scene/WhiteboardObject.h>
#include <scene/FreehandStroke.h>
#include <scene/HighlighterStroke.h>
#include <math/Types.h>
#include <math/AffineTransform.h>

namespace py = pybind11;
using namespace lb;

void bind_commands(py::module_& m) {

    // =========================================================================
    // Command (abstract base — not directly constructible)
    // =========================================================================
    py::class_<Command>(m, "Command")
        .def("description",     &Command::description)
        .def("memory_estimate", &Command::memory_estimate);

    // =========================================================================
    // CommandStack
    // =========================================================================
    py::class_<CommandStack>(m, "CommandStack")
        .def(py::init<>())
        .def(py::init<size_t>(), py::arg("max_depth"))
        .def("push", [](CommandStack& stack, Scene& scene, Command* cmd) {
            (void)cmd;
            throw std::runtime_error(
                "Use push_add, push_delete, push_move, push_transform, "
                "push_stroke, or push_compound instead.");
        })

        // Typed push helpers that construct the command and transfer ownership.
        .def("push_add", [](CommandStack& stack, Scene& scene, WhiteboardObject* obj, size_t z) {
            auto clone = obj->clone_exact();
            stack.push(scene, std::make_unique<AddObjectCmd>(std::move(clone), z));
        }, py::arg("scene"), py::arg("object"), py::arg("z_index") = SIZE_MAX)

        .def("push_delete", [](CommandStack& stack, Scene& scene, const UUID& id) {
            stack.push(scene, std::make_unique<DeleteObjectCmd>(id));
        }, py::arg("scene"), py::arg("object_id"))

        .def("push_move", [](CommandStack& stack, Scene& scene, const UUID& id, Vec2f delta) {
            stack.push(scene, std::make_unique<MoveObjectCmd>(id, delta));
        }, py::arg("scene"), py::arg("object_id"), py::arg("delta"))

        .def("push_transform", [](CommandStack& stack, Scene& scene,
                                   const UUID& id,
                                   const AffineTransform& old_t,
                                   const AffineTransform& new_t) {
            stack.push(scene, std::make_unique<TransformObjectCmd>(id, old_t, new_t));
        }, py::arg("scene"), py::arg("object_id"),
           py::arg("old_transform"), py::arg("new_transform"))

        .def("push_stroke", [](CommandStack& stack, Scene& scene, WhiteboardObject* stroke) {
            auto clone = stroke->clone_exact();
            stack.push(scene, std::make_unique<StrokeDrawCmd>(std::move(clone)));
        }, py::arg("scene"), py::arg("stroke"))

        .def("push_set_color", [](CommandStack& stack, Scene& scene,
                                   const std::vector<UUID>& ids, RGBA color) {
            stack.push(scene, std::make_unique<SetColorCmd>(ids, color));
        }, py::arg("scene"), py::arg("object_ids"), py::arg("color"))

        .def("push_set_background", [](CommandStack& stack, Scene& scene,
                                        const std::string& src) {
            stack.push(scene, std::make_unique<SetBackgroundCmd>(src));
        }, py::arg("scene"), py::arg("src"))

        .def("push_set_slide", [](CommandStack& stack, Scene& scene,
                                   const UUID& id, int32_t slide) {
            stack.push(scene, std::make_unique<SetSlideCmd>(id, slide));
        }, py::arg("scene"), py::arg("object_id"), py::arg("slide"))

        // One undo entry for a multi-part edit. ``actions`` is a list of
        // ("add", WhiteboardObject) / ("delete", UUID) tuples, executed in
        // list order; undo re-runs them in reverse. The canonical user is
        // the pixel eraser, whose single gesture splits N strokes into
        // M pieces — as individual pushes that was N+M undo steps, and
        // pressing Undo once resurrected an original on top of its own
        // pieces. (This replaces a stub that always threw; the Python
        // caller keeps a fallback for .pyds built before it existed.)
        .def("push_compound", [](CommandStack& stack, Scene& scene,
                                  const std::string& label,
                                  const py::list& actions) {
            auto compound = std::make_unique<CompoundCmd>(label);
            for (auto item : actions) {
                auto tup  = item.cast<py::tuple>();
                auto kind = tup[0].cast<std::string>();
                if (kind == "add") {
                    auto* obj = tup[1].cast<WhiteboardObject*>();
                    compound->add(std::make_unique<AddObjectCmd>(
                        obj->clone_exact(), SIZE_MAX));
                } else if (kind == "delete") {
                    compound->add(std::make_unique<DeleteObjectCmd>(
                        tup[1].cast<UUID>()));
                } else {
                    throw std::runtime_error(
                        "push_compound: unknown action '" + kind + "'");
                }
            }
            if (compound->empty()) return;
            stack.push(scene, std::move(compound));
        }, py::arg("scene"), py::arg("label"), py::arg("actions"))

        .def("push_compound_cmd", [](CommandStack& stack, Scene& scene,
                                      CompoundCmd* compound) {
            // Clone the compound command so Python doesn't lose its reference.
            auto cmd = std::make_unique<CompoundCmd>(compound->label());
            // Unfortunately we can't move sub-commands out of the const
            // compound, so this path requires the caller to use the
            // stack.begin_compound / end_compound pattern instead.
            // For now, execute the compound directly.
            (void)compound;
            throw std::runtime_error(
                "Use begin_compound / add_to_compound / end_compound pattern.");
        })

        .def("undo", &CommandStack::undo, py::arg("scene"))
        .def("redo", &CommandStack::redo, py::arg("scene"))

        .def_property_readonly("can_undo",   &CommandStack::can_undo)
        .def_property_readonly("can_redo",   &CommandStack::can_redo)
        .def_property_readonly("undo_count", &CommandStack::undo_count)
        .def_property_readonly("redo_count", &CommandStack::redo_count)

        .def("next_undo_description", &CommandStack::next_undo_description)
        .def("next_redo_description", &CommandStack::next_redo_description)

        .def_property("max_depth",
            &CommandStack::max_depth,
            &CommandStack::set_max_depth)
        .def_property("max_memory",
            &CommandStack::max_memory,
            &CommandStack::set_max_memory)
        .def_property_readonly("current_memory", &CommandStack::current_memory)

        .def("clear",      &CommandStack::clear)
        .def("clear_redo", &CommandStack::clear_redo)

        .def("set_change_callback", &CommandStack::set_change_callback);

    // =========================================================================
    // AddObjectCmd
    // =========================================================================
    py::class_<AddObjectCmd, Command>(m, "AddObjectCmd")
        .def(py::init([](WhiteboardObject* obj, size_t z) {
            return new AddObjectCmd(obj->clone_exact(), z);
        }), py::arg("object"), py::arg("z_index") = SIZE_MAX)
        .def_property_readonly("object_id", &AddObjectCmd::object_id);

    // =========================================================================
    // DeleteObjectCmd
    // =========================================================================
    py::class_<DeleteObjectCmd, Command>(m, "DeleteObjectCmd")
        .def(py::init<UUID>(), py::arg("target_id"))
        .def_property_readonly("object_id", &DeleteObjectCmd::object_id);

    // =========================================================================
    // MoveObjectCmd
    // =========================================================================
    py::class_<MoveObjectCmd, Command>(m, "MoveObjectCmd")
        .def(py::init<UUID, Vec2f>(), py::arg("target_id"), py::arg("delta"))
        .def_property_readonly("object_id", &MoveObjectCmd::object_id)
        .def_property_readonly("delta",     &MoveObjectCmd::delta)
        .def("merge_delta",    &MoveObjectCmd::merge_delta)
        .def("can_merge_with", &MoveObjectCmd::can_merge_with);

    // =========================================================================
    // TransformObjectCmd
    // =========================================================================
    py::class_<TransformObjectCmd, Command>(m, "TransformObjectCmd")
        .def(py::init<UUID, AffineTransform, AffineTransform>(),
             py::arg("target_id"), py::arg("old_transform"), py::arg("new_transform"))
        .def_property_readonly("object_id",     &TransformObjectCmd::object_id)
        .def_property_readonly("old_transform", &TransformObjectCmd::old_transform)
        .def_property_readonly("new_transform", &TransformObjectCmd::new_transform)
        .def("update_new_transform",            &TransformObjectCmd::update_new_transform);

    // =========================================================================
    // StrokeDrawCmd
    // =========================================================================
    py::class_<StrokeDrawCmd, Command>(m, "StrokeDrawCmd")
        .def(py::init([](WhiteboardObject* stroke) {
            return new StrokeDrawCmd(stroke->clone_exact());
        }), py::arg("stroke"))
        .def_property_readonly("stroke_id",   &StrokeDrawCmd::stroke_id)
        .def_property_readonly("stroke_type", &StrokeDrawCmd::stroke_type);

    // =========================================================================
    // CompoundCmd
    // =========================================================================
    py::class_<CompoundCmd, Command>(m, "CompoundCmd")
        .def(py::init<>())
        .def(py::init<std::string>(), py::arg("label"))
        .def_property("label", &CompoundCmd::label, &CompoundCmd::set_label)
        .def_property_readonly("size",  &CompoundCmd::size)
        .def_property_readonly("empty", &CompoundCmd::empty)
        .def("command_at", &CompoundCmd::command_at, py::return_value_policy::reference);
}