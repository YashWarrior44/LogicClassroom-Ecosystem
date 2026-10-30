// =============================================================================
// MoveObjectCmd.cpp
// =============================================================================

#include "../../include/command/MoveObjectCmd.h"
#include "../../include/scene/Scene.h"

#include <sstream>

namespace lb {

MoveObjectCmd::MoveObjectCmd(UUID target_id, Vec2f delta)
    : id_(target_id), delta_(delta) {}

void MoveObjectCmd::execute(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    if (!obj) return;

    obj->translate(delta_.x, delta_.y);
    scene.mark_dirty();
}

void MoveObjectCmd::undo(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    if (!obj) return;

    obj->translate(-delta_.x, -delta_.y);
    scene.mark_dirty();
}

std::string MoveObjectCmd::description() const {
    std::ostringstream ss;
    ss << "Move (" << delta_.x << ", " << delta_.y << ")";
    return ss.str();
}

void MoveObjectCmd::merge_delta(Vec2f additional) {
    delta_ += additional;
}

bool MoveObjectCmd::can_merge_with(const Command& other) const {
    const auto* move = dynamic_cast<const MoveObjectCmd*>(&other);
    if (!move) return false;
    return move->id_ == id_;
}

} // namespace lb