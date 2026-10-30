// =============================================================================
// SetBackgroundCmd.cpp — Undoable per-page background image change.
// =============================================================================

#include "../../include/command/SetBackgroundCmd.h"
#include "../../include/scene/Scene.h"

namespace lb {

SetBackgroundCmd::SetBackgroundCmd(std::string new_src)
    : new_src_(std::move(new_src)) {}

void SetBackgroundCmd::execute(Scene& scene) {
    if (!captured_) {
        old_src_ = scene.bg_image();
        captured_ = true;
    }
    scene.set_bg_image(new_src_);
    scene.mark_dirty();
}

void SetBackgroundCmd::undo(Scene& scene) {
    scene.set_bg_image(old_src_);
    scene.mark_dirty();
}

std::string SetBackgroundCmd::description() const {
    return new_src_.empty() ? "Clear background" : "Set background image";
}

} // namespace lb
