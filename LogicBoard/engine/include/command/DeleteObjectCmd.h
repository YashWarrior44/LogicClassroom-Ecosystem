#pragma once
// =============================================================================
// DeleteObjectCmd.h — Removes a WhiteboardObject from the scene by UUID.
//
// execute(): removes the object, stashing it internally with its z-index.
// undo():    re-inserts the stashed object at its original z-index.
// =============================================================================

#include "Command.h"
#include "../scene/WhiteboardObject.h"

#include <cstddef>
#include <memory>
#include <string>

namespace lb {

class Scene;

class DeleteObjectCmd : public Command {
public:
    /// Construct with the UUID of the object to delete.
    explicit DeleteObjectCmd(UUID target_id);

    ~DeleteObjectCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;
    size_t memory_estimate() const override;

    const UUID& object_id() const { return id_; }

private:
    UUID                                id_;
    std::unique_ptr<WhiteboardObject>   stashed_;       // held when not in scene
    size_t                              original_z_ = 0;
    // Matches the guard field used by AddObjectCmd and StrokeDrawCmd:
    // ``true`` after a successful execute, ``false`` after undo. Without
    // this flag, calling execute() twice in a row (which shouldn't
    // happen from CommandStack but can from a faulty caller) would
    // have the second call's ``scene.remove(id_)`` return nullptr — and
    // that nullptr would OVERWRITE the still-valid ``stashed_`` from the
    // first call, permanently losing the object.
    bool                                executed_ = false;
};

} // namespace lb