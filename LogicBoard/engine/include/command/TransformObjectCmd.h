#pragma once
// =============================================================================
// TransformObjectCmd.h — Replaces an object's AffineTransform wholesale.
//
// Used for scale, rotate, and combined manipulations where storing the
// full before/after transforms is simpler than incremental deltas.
//
// execute(): sets the object's transform to `new_transform`.
// undo():    restores `old_transform`.
//
// Optionally captures before/after for additional per-object properties
// that may change during a resize.
// =============================================================================

#include "Command.h"
#include "../math/AffineTransform.h"
#include "../scene/WhiteboardObject.h"

#include <string>

namespace lb {

class Scene;

class TransformObjectCmd : public Command {
public:
    /// Construct with the target UUID and the old + new transforms.
    TransformObjectCmd(UUID target_id,
                       AffineTransform old_transform,
                       AffineTransform new_transform);

    ~TransformObjectCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;

    const UUID& object_id() const { return id_; }
    const AffineTransform& old_transform() const { return old_; }
    const AffineTransform& new_transform() const { return new_; }

    /// Update the "new" transform in-place.  Used during interactive
    /// manipulation (drag-rotate, drag-scale) so the command tracks the
    /// latest state without pushing multiple commands.
    void update_new_transform(const AffineTransform& t);

private:
    UUID            id_;
    AffineTransform old_;
    AffineTransform new_;
};

} // namespace lb