#pragma once
// =============================================================================
// AddObjectCmd.h — Adds a WhiteboardObject to the scene.
//
// execute(): inserts the object at a specified z-index (or top).
// undo():    removes the object by UUID and stashes it internally.
// redo():    re-inserts the stashed object at the original z-index.
// =============================================================================

#include "Command.h"
#include "../scene/WhiteboardObject.h"

#include <cstddef>
#include <memory>
#include <string>

namespace lb {

class Scene;

class AddObjectCmd : public Command {
public:
    /// Construct with the object to add.  Ownership transfers to the command
    /// until execute() moves it into the scene.
    /// `z_index` = SIZE_MAX means "add on top" (default).
    explicit AddObjectCmd(std::unique_ptr<WhiteboardObject> object,
                          size_t z_index = SIZE_MAX);

    ~AddObjectCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;
    size_t memory_estimate() const override;

    /// UUID of the object being added (valid after construction).
    const UUID& object_id() const { return id_; }

private:
    UUID                                id_;
    std::unique_ptr<WhiteboardObject>   object_;    // held when not in scene
    size_t                              z_index_;
    bool                                executed_ = false;
};

} // namespace lb