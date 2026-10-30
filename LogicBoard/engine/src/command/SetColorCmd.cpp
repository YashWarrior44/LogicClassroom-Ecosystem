// =============================================================================
// SetColorCmd.cpp — Multi-object color change with exact undo.
// =============================================================================

#include "../../include/command/SetColorCmd.h"
#include "../../include/scene/Scene.h"
#include "../../include/scene/WhiteboardObject.h"

#include <sstream>

namespace lb {

SetColorCmd::SetColorCmd(std::vector<UUID> ids, RGBA new_color)
    : ids_(std::move(ids)), new_color_(new_color) {}

void SetColorCmd::execute(Scene& scene) {
    if (!captured_) {
        // First run: snapshot each object's current colors.
        old_stroke_.reserve(ids_.size());
        old_fill_.reserve(ids_.size());
        had_fill_.reserve(ids_.size());
        for (const auto& id : ids_) {
            WhiteboardObject* obj = scene.find_mut(id);
            if (obj) {
                old_stroke_.push_back(obj->stroke_color());
                RGBA fc = obj->fill_color();
                old_fill_.push_back(fc);
                had_fill_.push_back(fc.a > 0);
            } else {
                old_stroke_.push_back(RGBA::transparent());
                old_fill_.push_back(RGBA::transparent());
                had_fill_.push_back(false);
            }
        }
        captured_ = true;
    }

    for (size_t i = 0; i < ids_.size(); ++i) {
        WhiteboardObject* obj = scene.find_mut(ids_[i]);
        if (!obj) continue;
        obj->set_stroke_color(new_color_);
        // If the object had a visible fill, recolor it too (preserve alpha).
        if (i < had_fill_.size() && had_fill_[i]) {
            RGBA nf = new_color_;
            nf.a = old_fill_[i].a;
            obj->set_fill_color(nf);
        }
    }
    scene.mark_dirty();
}

void SetColorCmd::undo(Scene& scene) {
    for (size_t i = 0; i < ids_.size(); ++i) {
        WhiteboardObject* obj = scene.find_mut(ids_[i]);
        if (!obj) continue;
        if (i < old_stroke_.size()) obj->set_stroke_color(old_stroke_[i]);
        if (i < old_fill_.size() && had_fill_[i]) obj->set_fill_color(old_fill_[i]);
    }
    scene.mark_dirty();
}

std::string SetColorCmd::description() const {
    std::ostringstream os;
    os << "Set color of " << ids_.size() << " object(s)";
    return os.str();
}

} // namespace lb
