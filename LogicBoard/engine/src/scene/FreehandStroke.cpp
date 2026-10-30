// =============================================================================
// FreehandStroke.cpp — Freehand pen stroke: polyline of sampled points with
// optional per-point pressure, Ramer-Douglas-Peucker simplification,
// cached bounding box, segment-distance hit-testing, and JSON serialization.
// =============================================================================

#include "../../include/scene/FreehandStroke.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <stack>
#include <utility>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

FreehandStroke::FreehandStroke()
    : WhiteboardObject() {
    set_line_cap(LineCap::Round);
    set_line_join(LineJoin::Round);
}

FreehandStroke::FreehandStroke(UUID id)
    : WhiteboardObject(id) {
    set_line_cap(LineCap::Round);
    set_line_join(LineJoin::Round);
}

// =============================================================================
// Point mutation
// =============================================================================

void FreehandStroke::reserve(size_t estimated_points) {
    points_.reserve(estimated_points);
    pressures_.reserve(estimated_points);
}

void FreehandStroke::add_point(Vec2f pt, float pressure) {
    points_.push_back(pt);

    // If caller supplies pressure (>= 0), track it.  If the vector was
    // previously empty (no pressure data), back-fill with 1.0 for
    // already-added points so arrays stay in sync.
    if (pressure >= 0.0f) {
        if (pressures_.empty() && points_.size() > 1) {
            pressures_.resize(points_.size() - 1, 1.0f);
        }
        pressures_.push_back(pressure);
    } else if (!pressures_.empty()) {
        // Pressure was being tracked but this sample has none — default 1.0.
        pressures_.push_back(1.0f);
    }

    invalidate_cache();
}

void FreehandStroke::set_points(std::vector<Vec2f> pts) {
    points_ = std::move(pts);
    pressures_.clear();
    invalidate_cache();
}

void FreehandStroke::set_points(std::vector<Vec2f> pts, std::vector<float> prs) {
    points_ = std::move(pts);
    if (prs.size() == points_.size()) {
        pressures_ = std::move(prs);
    } else {
        pressures_.clear();
    }
    invalidate_cache();
}

void FreehandStroke::pop_back() {
    if (points_.empty()) return;
    points_.pop_back();
    if (!pressures_.empty()) pressures_.pop_back();
    invalidate_cache();
}

void FreehandStroke::clear() {
    points_.clear();
    pressures_.clear();
    invalidate_cache();
}

// =============================================================================
// Ramer-Douglas-Peucker simplification (iterative, no recursion)
// =============================================================================

namespace {

// Perpendicular distance from point P to the line segment A–B.
float perpendicular_distance(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) {
        return p.distance(a);
    }
    // Project P onto AB and clamp to [0, 1] for segment distance.
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance(proj);
}

// Iterative RDP returning a bool mask of which indices to keep.
std::vector<bool> rdp_mask(const std::vector<Vec2f>& pts, float epsilon) {
    size_t n = pts.size();
    std::vector<bool> keep(n, false);

    // A tolerance below zero is not a finer simplification, it is a hang.
    //
    // On a span whose points are all collinear every perpendicular distance
    // is 0, so max_dist stays 0 and max_idx is never moved off `start`. The
    // split test then reads `0 > -1` -- true -- and pushes {start, max_idx}
    // and {max_idx, end}, which is {start, start} (discarded) and
    // {start, end}: the very range just popped. The stack never shrinks and
    // the call never returns. A straight line simplified with a negative
    // epsilon froze the process outright.
    //
    // Written as `!(epsilon > 0)` rather than `epsilon < 0` so NaN -- where
    // every comparison is false -- lands here too instead of falling through
    // into the same trap.
    if (!(epsilon > 0.0f)) epsilon = 0.0f;

    if (n < 3) {
        // Keep everything if 0, 1, or 2 points.
        std::fill(keep.begin(), keep.end(), true);
        return keep;
    }

    keep[0] = true;
    keep[n - 1] = true;

    // Stack of (start, end) index pairs to process.
    std::stack<std::pair<size_t, size_t>> stack;
    stack.push({0, n - 1});

    while (!stack.empty()) {
        auto [start, end] = stack.top();
        stack.pop();

        if (end <= start + 1) continue;

        float max_dist = 0.0f;
        size_t max_idx = start;

        for (size_t i = start + 1; i < end; ++i) {
            float d = perpendicular_distance(pts[i], pts[start], pts[end]);
            if (d > max_dist) {
                max_dist = d;
                max_idx = i;
            }
        }

        // The index must be strictly INSIDE the span, or the two halves are
        // not smaller than the whole. Clamping epsilon above already rules
        // out the case that broke this, but termination should not rest on
        // an argument about the tolerance -- this holds for any input.
        if (max_dist > epsilon && max_idx > start && max_idx < end) {
            keep[max_idx] = true;
            stack.push({start, max_idx});
            stack.push({max_idx, end});
        }
    }

    return keep;
}

} // anonymous namespace

void FreehandStroke::simplify(float epsilon) {
    if (points_.size() < 3) return;

    std::vector<bool> keep = rdp_mask(points_, epsilon);

    std::vector<Vec2f> new_points;
    std::vector<float> new_pressures;
    new_points.reserve(points_.size());
    if (!pressures_.empty()) new_pressures.reserve(pressures_.size());

    for (size_t i = 0; i < points_.size(); ++i) {
        if (keep[i]) {
            new_points.push_back(points_[i]);
            if (i < pressures_.size()) {
                new_pressures.push_back(pressures_[i]);
            }
        }
    }

    points_ = std::move(new_points);

    if (!new_pressures.empty() && new_pressures.size() == points_.size()) {
        pressures_ = std::move(new_pressures);
    } else {
        pressures_.clear();
    }

    invalidate_cache();
}

// =============================================================================
// Bounding box
// =============================================================================

void FreehandStroke::recompute_bbox() const {
    if (points_.empty()) {
        cached_bbox_ = AABB();
        bbox_dirty_ = false;
        return;
    }

    cached_bbox_ = AABB::from_points(points_.data(), points_.size());

    // Expand by half the stroke width so the full rendered stroke is enclosed.
    float half_w = stroke_width() * 0.5f;
    cached_bbox_ = cached_bbox_.expanded(half_w);

    bbox_dirty_ = false;
}

AABB FreehandStroke::local_bounding_box() const {
    if (bbox_dirty_) recompute_bbox();
    return cached_bbox_;
}

// =============================================================================
// Hit-test: minimum distance from point to any polyline segment
// =============================================================================

namespace {

// Squared distance from point P to line segment A–B.
float point_segment_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance_sq(a);

    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance_sq(proj);
}

} // anonymous namespace

bool FreehandStroke::hit_test_local(Vec2f local_point, float tolerance) const {
    if (points_.empty()) return false;

    // Quick AABB rejection.
    AABB bb = local_bounding_box();
    if (!bb.expanded(tolerance).contains(local_point)) return false;

    // Effective hit radius = half stroke width + caller tolerance.
    float hit_radius = stroke_width() * 0.5f + tolerance;
    float hit_radius_sq = hit_radius * hit_radius;

    // Single-point stroke: distance to that point.
    if (points_.size() == 1) {
        return local_point.distance_sq(points_[0]) <= hit_radius_sq;
    }

    // Check every segment.
    for (size_t i = 0; i + 1 < points_.size(); ++i) {
        if (point_segment_dist_sq(local_point, points_[i], points_[i + 1]) <= hit_radius_sq) {
            return true;
        }
    }

    return false;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> FreehandStroke::clone() const {
    auto copy = std::make_unique<FreehandStroke>();
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->points_    = points_;
    copy->pressures_ = pressures_;
    copy->invalidate_cache();
    return copy;
}

std::unique_ptr<WhiteboardObject> FreehandStroke::clone_exact() const {
    auto copy = std::make_unique<FreehandStroke>(id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->points_    = points_;
    copy->pressures_ = pressures_;
    copy->invalidate_cache();
    return copy;
}

// =============================================================================
// JSON serialization
// =============================================================================

void FreehandStroke::serialize_properties(json& j) const {
    // Points as array of [x, y] pairs.
    json pts = json::array();
    pts.get_ref<json::array_t&>().reserve(points_.size());
    for (const auto& p : points_) {
        pts.push_back(json::array({p.x, p.y}));
    }
    j["points"] = std::move(pts);

    // Pressures (only if non-empty).
    if (!pressures_.empty()) {
        json prs = json::array();
        prs.get_ref<json::array_t&>().reserve(pressures_.size());
        for (float pr : pressures_) {
            prs.push_back(pr);
        }
        j["pressures"] = std::move(prs);
    }
}

void FreehandStroke::deserialize_properties(const json& j) {
    points_.clear();
    pressures_.clear();

    if (j.contains("points") && j["points"].is_array()) {
        const auto& arr = j["points"];
        points_.reserve(arr.size());
        for (const auto& pt : arr) {
            if (pt.is_array() && pt.size() >= 2) {
                points_.push_back({pt[0].get<float>(), pt[1].get<float>()});
            }
        }
    }

    if (j.contains("pressures") && j["pressures"].is_array()) {
        const auto& arr = j["pressures"];
        pressures_.reserve(arr.size());
        for (const auto& val : arr) {
            if (val.is_number()) {
                pressures_.push_back(val.get<float>());
            }
        }
        // Discard if length mismatch.
        if (pressures_.size() != points_.size()) {
            pressures_.clear();
        }
    }

    invalidate_cache();
}

} // namespace lb