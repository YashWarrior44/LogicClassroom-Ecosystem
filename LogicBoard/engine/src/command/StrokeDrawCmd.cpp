// =============================================================================
// StrokeDrawCmd.cpp
// =============================================================================

#include "../../include/command/StrokeDrawCmd.h"
#include "../../include/scene/Scene.h"
#include "../../include/scene/FreehandStroke.h"
#include "../../include/scene/HighlighterStroke.h"

namespace lb {

StrokeDrawCmd::StrokeDrawCmd(std::unique_ptr<WhiteboardObject> stroke) {
    if (stroke) {
        id_ = stroke->id();
        stroke_ = std::move(stroke);
    }
}

void StrokeDrawCmd::execute(Scene& scene) {
    if (!stroke_) return;

    scene.add(std::move(stroke_));
    executed_ = true;
}

void StrokeDrawCmd::undo(Scene& scene) {
    if (!executed_) return;

    stroke_ = scene.remove(id_);
    executed_ = false;
}

std::string StrokeDrawCmd::description() const {
    const char* type = stroke_type();
    if (type) {
        return std::string("Draw ") + type;
    }
    return "Draw stroke";
}

size_t StrokeDrawCmd::memory_estimate() const {
    size_t base = 96;

    // Estimate based on the stroke's point count when we hold it.
    if (stroke_) {
        // Check if it's a freehand stroke.
        if (const auto* fs = dynamic_cast<const FreehandStroke*>(stroke_.get())) {
            // Each point is 2 floats (8 bytes) + optional pressure (4 bytes).
            base += fs->point_count() * 12;
        } else if (const auto* hs = dynamic_cast<const HighlighterStroke*>(stroke_.get())) {
            base += hs->point_count() * 8;
        } else {
            base += stroke_->memory_estimate();
        }
    }

    return base;
}

const char* StrokeDrawCmd::stroke_type() const {
    if (stroke_) return stroke_->type_name();
    return nullptr;
}

} // namespace lb