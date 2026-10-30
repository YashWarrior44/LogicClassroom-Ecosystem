#pragma once
// =============================================================================
// StrokeDrawCmd.h — Captures a completed freehand or highlighter stroke.
//
// Created when the user lifts the pen/finger after drawing.  The command
// owns a clone of the fully-formed stroke object.
//
// execute(): adds the stroke to the scene.
// undo():    removes it by UUID.
// redo():    re-inserts the clone.
//
// This is functionally an AddObjectCmd specialised for strokes, but
// carrying a distinct type name so the UI can show "Undo Stroke" vs
// "Undo Add Shape" and so the command stack can apply stroke-specific
// policies (e.g. auto-simplification on push).
// =============================================================================

#include "Command.h"
#include "../scene/WhiteboardObject.h"

#include <cstddef>
#include <memory>
#include <string>

namespace lb {

class Scene;

class StrokeDrawCmd : public Command {
public:
    /// Construct with the completed stroke object.  Ownership transfers to
    /// the command.  `stroke` must be a FreehandStroke or HighlighterStroke
    /// (or any WhiteboardObject — the command is type-agnostic internally).
    explicit StrokeDrawCmd(std::unique_ptr<WhiteboardObject> stroke);

    ~StrokeDrawCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;
    size_t memory_estimate() const override;

    const UUID& stroke_id() const { return id_; }

    /// The type name of the held stroke ("freehand", "highlighter", …).
    const char* stroke_type() const;

private:
    UUID                                id_;
    std::unique_ptr<WhiteboardObject>   stroke_;   // held when not in scene
    bool                                executed_ = false;
};

} // namespace lb