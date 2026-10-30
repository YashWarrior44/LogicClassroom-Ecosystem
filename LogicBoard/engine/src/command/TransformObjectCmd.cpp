// =============================================================================
// TransformObjectCmd.cpp
// =============================================================================

#include "../../include/command/TransformObjectCmd.h"
#include "../../include/scene/Scene.h"

namespace lb {

TransformObjectCmd::TransformObjectCmd(UUID target_id,
                                       AffineTransform old_transform,
                                       AffineTransform new_transform)
    : id_(target_id), old_(old_transform), new_(new_transform) {}

void TransformObjectCmd::execute(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    if (!obj) return;

    obj->set_transform(new_);
    scene.mark_dirty();
}

void TransformObjectCmd::undo(Scene& scene) {
    WhiteboardObject* obj = scene.find_mut(id_);
    if (!obj) return;

    obj->set_transform(old_);
    scene.mark_dirty();
}

std::string TransformObjectCmd::description() const {
    return "Transform object";
}

void TransformObjectCmd::update_new_transform(const AffineTransform& t) {
    new_ = t;
}

} // namespace lb