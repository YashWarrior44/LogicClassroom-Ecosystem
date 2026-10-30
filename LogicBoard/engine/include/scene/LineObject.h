#pragma once
// =============================================================================
// LineObject.h — A straight line segment between two endpoints.
//
// JSON:
// {
//   "type": "line",
//   "start": [x, y],
//   "end":   [x, y],
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"

namespace lb {

class LineObject : public WhiteboardObject {
public:
    LineObject();
    explicit LineObject(UUID id);
    LineObject(Vec2f start, Vec2f end);
    LineObject(Vec2f start, Vec2f end, UUID id);
    ~LineObject() override = default;

    // ---- Geometry ----

    Vec2f start() const { return start_; }
    void set_start(Vec2f p) { start_ = p; }

    Vec2f end() const { return end_; }
    void set_end(Vec2f p) { end_ = p; }

    /// Length of the line segment in local coordinates.
    float length() const { return start_.distance(end_); }

    /// Direction vector (start → end), normalized.
    Vec2f direction() const { return (end_ - start_).normalized(); }

    /// Midpoint of the segment.
    Vec2f midpoint() const { return (start_ + end_) * 0.5f; }

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test: checks minimum distance from `local_point` to the segment.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "line"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f start_;
    Vec2f end_;
};

} // namespace lb