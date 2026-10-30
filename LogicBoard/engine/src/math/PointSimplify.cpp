#include "../../include/math/PointSimplify.h"
#include <cmath>
#include <algorithm>
#include <stack>
#include <utility>

namespace lb {

namespace {

float perp_dist(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance(proj);
}

} // anonymous namespace

std::vector<bool> simplify_mask(const std::vector<Vec2f>& pts, float epsilon) {
    size_t n = pts.size();
    std::vector<bool> keep(n, false);

    // A tolerance below zero is not a finer simplification, it is a hang.
    //
    // On a span whose points are all collinear every perpendicular distance
    // is 0, so max_dist stays 0 and max_idx is never moved off `start`. The
    // split test then reads `0 > -1` -- true -- and pushes {start, max_idx}
    // and {max_idx, end}, which is {start, start} (discarded) and
    // {start, end}: the very range just popped. The stack never shrinks and
    // the call never returns. A straight line drawn with a negative epsilon
    // froze the process outright.
    //
    // Written as `!(epsilon > 0)` rather than `epsilon < 0` so that NaN --
    // where every comparison is false -- lands here too instead of falling
    // through into the same trap.
    if (!(epsilon > 0.0f)) epsilon = 0.0f;

    if (n < 3) {
        std::fill(keep.begin(), keep.end(), true);
        return keep;
    }

    keep[0] = true;
    keep[n - 1] = true;

    std::stack<std::pair<size_t, size_t>> stack;
    stack.push({0, n - 1});

    while (!stack.empty()) {
        auto [start, end] = stack.top();
        stack.pop();

        if (end <= start + 1) continue;

        float max_dist = 0.0f;
        size_t max_idx = start;

        for (size_t i = start + 1; i < end; ++i) {
            float d = perp_dist(pts[i], pts[start], pts[end]);
            if (d > max_dist) {
                max_dist = d;
                max_idx = i;
            }
        }

        // The index must be strictly INSIDE the span for the two halves to
        // be smaller than the whole. Clamping epsilon above already rules
        // out the case that broke this, but the recursion's termination
        // should not rest on an argument about the tolerance -- it holds
        // here, structurally, for any input.
        if (max_dist > epsilon && max_idx > start && max_idx < end) {
            keep[max_idx] = true;
            stack.push({start, max_idx});
            stack.push({max_idx, end});
        }
    }

    return keep;
}

std::vector<Vec2f> simplify_polyline(const std::vector<Vec2f>& pts, float epsilon) {
    auto mask = simplify_mask(pts, epsilon);
    std::vector<Vec2f> result;
    result.reserve(pts.size());
    for (size_t i = 0; i < pts.size(); ++i) {
        if (mask[i]) result.push_back(pts[i]);
    }
    return result;
}

} // namespace lb
