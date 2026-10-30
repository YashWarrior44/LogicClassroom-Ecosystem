// =============================================================================
// DeleteObjectCmd.cpp
// =============================================================================

#include "../../include/command/DeleteObjectCmd.h"
#include "../../include/scene/Scene.h"

namespace lb {

DeleteObjectCmd::DeleteObjectCmd(UUID target_id)
    : id_(target_id) {}

void DeleteObjectCmd::execute(Scene& scene) {
    // Idempotent — a second execute() with the object already stashed
    // must NOT try to remove again. The previous version's
    // ``scene.remove(id_)`` would return nullptr on the second call and
    // silently overwrite ``stashed_`` with that nullptr, permanently
    // losing the object we were supposed to be able to un-delete.
    if (executed_) return;

    // Record the current z-index before removal.
    int idx = scene.find_index(id_);
    if (idx >= 0) {
        original_z_ = static_cast<size_t>(idx);
    }

    stashed_ = scene.remove(id_);
    // Only flip the guard if we actually got the object out — otherwise
    // the object was never in the scene (or was already gone from a
    // previous session), and a later undo would have nothing to restore.
    if (stashed_) {
        executed_ = true;
    }
}

void DeleteObjectCmd::undo(Scene& scene) {
    if (!executed_ || !stashed_) return;

    scene.add_at(std::move(stashed_), original_z_);
    executed_ = false;
}

std::string DeleteObjectCmd::description() const {
    if (stashed_) {
        return std::string("Delete ") + stashed_->type_name();
    }
    return "Delete object";
}

size_t DeleteObjectCmd::memory_estimate() const {
    size_t base = 96;
    if (stashed_) {
        base += stashed_->memory_estimate();
    }
    return base;
}

} // namespace lb