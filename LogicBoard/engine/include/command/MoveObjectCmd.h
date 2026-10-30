#pragma once
// =============================================================================
// MoveObjectCmd.h — Translates an object by a (dx, dy) delta.
//
// execute(): prepends a translation to the object's transform.
// undo():    prepends the inverse translation.
//
// Supports merging consecutive small moves into a single command (optional)
// to avoid flooding the history with per-pixel drag events.
// =============================================================================

#include "Command.h"
#include "../math/Types.h"
#include "../scene/WhiteboardObject.h"

#include <string>

namespace lb {

class Scene;

class MoveObjectCmd : public Command {
public:
    MoveObjectCmd(UUID target_id, Vec2f delta);
    ~MoveObjectCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;

    const UUID& object_id() const { return id_; }
    Vec2f delta() const { return delta_; }

    /// Accumulate an additional delta into this command.  Useful for
    /// coalescing drag events: instead of pushing a new command per
    /// mouse-move, the caller can merge into the active command while
    /// the drag is in progress.
    void merge_delta(Vec2f additional);

    /// Returns true if `other` is also a MoveObjectCmd for the same object,
    /// meaning it can be merged.
    bool can_merge_with(const Command& other) const;

private:
    UUID  id_;
    Vec2f delta_;
};

} // namespace lb