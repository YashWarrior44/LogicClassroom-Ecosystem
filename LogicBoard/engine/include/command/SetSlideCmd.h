#pragma once
// =============================================================================
// SetSlideCmd.h — Change a PptObject's current slide index (undoable).
// Stores the old slide index for exact undo.
// =============================================================================

#include "Command.h"
#include "../math/Types.h"
#include "../scene/WhiteboardObject.h"

#include <string>
#include <cstdint>

namespace lb {

class Scene;

class SetSlideCmd : public Command {
public:
    SetSlideCmd(UUID target_id, int32_t new_slide);
    ~SetSlideCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;

private:
    UUID id_;
    int32_t new_slide_;
    int32_t old_slide_ = 0;
    bool captured_ = false;
};

} // namespace lb
