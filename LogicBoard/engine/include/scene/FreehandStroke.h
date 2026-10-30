#pragma once
// =============================================================================
// FreehandStroke.h — A freehand pen stroke represented as a series of sampled
// points with optional per-point pressure.
//
// This is the workhorse object of any whiteboard.  The stroke is stored as a
// dense vector of (x, y) samples captured during drawing.  When the stroke is
// finalized (mouse/touch up), Ramer-Douglas-Peucker simplification can be
// applied to reduce point count without visible quality loss.
//
// JSON representation:
// {
//   "type": "freehand",
//   "points": [[x0,y0], [x1,y1], ...],
//   "pressures": [1.0, 0.95, ...],      // optional, same length as points
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <vector>

namespace lb {

class FreehandStroke : public WhiteboardObject {
public:
    FreehandStroke();
    explicit FreehandStroke(UUID id);
    ~FreehandStroke() override = default;

    // ---- Point data ----

    /// All sample points in local coordinates.
    const std::vector<Vec2f>& points() const { return points_; }

    /// Per-point pressure values in [0, 1].  May be empty if the input
    /// device does not report pressure (in which case uniform width is used).
    const std::vector<float>& pressures() const { return pressures_; }

    /// Number of sample points.
    size_t point_count() const { return points_.size(); }

    /// True when the stroke has no geometry.
    bool empty() const { return points_.empty(); }

    // ---- Building a stroke incrementally ----

    /// Reserve space for an estimated number of points (avoids reallocation
    /// during live drawing).
    void reserve(size_t estimated_points);

    /// Append a new sample to the stroke.  `pressure` is optional (pass
    /// negative to omit pressure data entirely).
    void add_point(Vec2f pt, float pressure = -1.0f);

    /// Replace the entire point array (e.g., after simplification).
    void set_points(std::vector<Vec2f> pts);

    /// Replace points and pressures together (must be same length, or
    /// pressures can be empty).
    void set_points(std::vector<Vec2f> pts, std::vector<float> pressures);

    /// Remove the last point (useful for cancelling a stroke in progress).
    void pop_back();

    /// Clear all points and pressures.
    void clear();

    // ---- Simplification ----

    /// Apply Ramer-Douglas-Peucker simplification in-place.
    /// `epsilon` is the maximum allowed deviation in local-space pixels.
    /// Pressure data is downsampled to match the simplified point indices.
    void simplify(float epsilon = 1.0f);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test: checks if `local_point` is within `tolerance` of any
    /// segment in the polyline.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "freehand"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    std::vector<Vec2f>  points_;
    std::vector<float>  pressures_;

    /// Cached bounding box (invalidated on mutation).
    mutable AABB        cached_bbox_;
    mutable bool        bbox_dirty_ = true;

    void invalidate_cache() { bbox_dirty_ = true; }
    void recompute_bbox() const;
};

} // namespace lb