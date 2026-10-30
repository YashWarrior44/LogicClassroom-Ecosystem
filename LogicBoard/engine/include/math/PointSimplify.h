#pragma once
// =============================================================================
// PointSimplify.h — Ramer-Douglas-Peucker polyline simplification.
//
// Standalone utility so it can be used outside of FreehandStroke if needed.
// =============================================================================

#include "Types.h"
#include <vector>

namespace lb {

/// Simplify a polyline using the Ramer-Douglas-Peucker algorithm.
/// Returns a new vector containing only the retained points.
/// `epsilon` is the maximum perpendicular deviation allowed.
std::vector<Vec2f> simplify_polyline(const std::vector<Vec2f>& points, float epsilon);

/// Returns a boolean mask (same length as `points`) where true = keep.
std::vector<bool> simplify_mask(const std::vector<Vec2f>& points, float epsilon);

} // namespace lb
