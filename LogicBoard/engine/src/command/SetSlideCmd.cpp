// =============================================================================
// SetSlideCmd.cpp — Undoable PptObject slide index change.
// =============================================================================

#include "../../include/command/SetSlideCmd.h"
#include "../../include/scene/Scene.h"
#include "../../include/scene/PptObject.h"

#include <sstream>

namespace lb {

SetSlideCmd::SetSlideCmd(UUID target_id, int32_t new_slide)
    : id_(target_id), new_slide_(new_slide) {}

void SetSlideCmd::execute(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    auto* ppt = dynamic_cast<PptObject*>(obj);
    if (!ppt) return;
    if (!captured_) {
        old_slide_ = ppt->current_slide();
        captured_ = true;
    }
    ppt->set_current_slide(new_slide_);
    scene.mark_dirty();
}

void SetSlideCmd::undo(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    auto* ppt = dynamic_cast<PptObject*>(obj);
    if (!ppt) return;
    ppt->set_current_slide(old_slide_);
    scene.mark_dirty();
}

std::string SetSlideCmd::description() const {
    std::ostringstream os;
    os << "Go to slide " << (new_slide_ + 1);
    return os.str();
}

} // namespace lb
