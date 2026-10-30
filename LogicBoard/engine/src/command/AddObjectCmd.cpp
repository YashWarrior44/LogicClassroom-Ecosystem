// =============================================================================
// AddObjectCmd.cpp
// =============================================================================

#include "../../include/command/AddObjectCmd.h"
#include "../../include/scene/Scene.h"

namespace lb {

AddObjectCmd::AddObjectCmd(std::unique_ptr<WhiteboardObject> object, size_t z_index)
    : z_index_(z_index) {
    if (object) {
        id_ = object->id();
        object_ = std::move(object);
    }
}

void AddObjectCmd::execute(Scene& scene) {
    if (!object_) return;

    if (z_index_ == SIZE_MAX) {
        scene.add(std::move(object_));
    } else {
        scene.add_at(std::move(object_), z_index_);
    }
    executed_ = true;
}

void AddObjectCmd::undo(Scene& scene) {
    if (!executed_) return;

    // Remember current z-index so redo re-inserts at the same position.
    int idx = scene.find_index(id_);
    if (idx >= 0) {
        z_index_ = static_cast<size_t>(idx);
    }

    object_ = scene.remove(id_);
    executed_ = false;
}

std::string AddObjectCmd::description() const {
    if (object_) {
        return std::string("Add ") + object_->type_name();
    }
    return "Add object";
}

size_t AddObjectCmd::memory_estimate() const {
    // Base overhead + the stashed object when it's not in the scene.
    size_t base = 96;
    if (object_) {
        base += object_->memory_estimate();
    }
    return base;
}

} // namespace lb