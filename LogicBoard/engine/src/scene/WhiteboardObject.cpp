// =============================================================================
// WhiteboardObject.cpp — Implementation of UUID, base class serialization,
// and the from_json() factory that dispatches to concrete shape types.
// =============================================================================

#include "../../include/scene/WhiteboardObject.h"

// Concrete types — needed by the from_json() factory.
#include "../../include/scene/FreehandStroke.h"
#include "../../include/scene/HighlighterStroke.h"
#include "../../include/scene/LineObject.h"
#include "../../include/scene/RectObject.h"
#include "../../include/scene/EllipseObject.h"
#include "../../include/scene/CircleObject.h"
#include "../../include/scene/ArrowObject.h"
#include "../../include/scene/PolygonObject.h"
#include "../../include/scene/TriangleObject.h"
#include "../../include/scene/ShapeObject.h"
#include "../../include/scene/ImageObject.h"
#include "../../include/scene/PptObject.h"

#include <nlohmann/json.hpp>

#include <random>
#include <sstream>
#include <iomanip>
#include <cstring>
#include <stdexcept>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// UUID
// =============================================================================

UUID UUID::generate() {
    // Thread-local Mersenne Twister seeded from hardware entropy.
    thread_local std::mt19937_64 rng([] {
        std::random_device rd;
        // Mix multiple entropy samples to reduce risk of poor seeds.
        uint64_t seed = static_cast<uint64_t>(rd()) << 32 | rd();
        return std::mt19937_64(seed);
    }());

    std::uniform_int_distribution<uint64_t> dist;

    uint64_t hi = dist(rng);
    uint64_t lo = dist(rng);

    // Set version 4 (bits 12-15 of hi byte 6).
    hi = (hi & 0xFFFFFFFFFFFF0FFFULL) | 0x0000000000004000ULL;
    // Set variant 1 (bits 6-7 of lo byte 0).
    lo = (lo & 0x3FFFFFFFFFFFFFFFULL) | 0x8000000000000000ULL;

    return {hi, lo};
}

UUID UUID::from_string(const std::string& str) {
    // Expected format: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" (36 chars)
    // or the 32-char hex without dashes.
    std::string hex;
    hex.reserve(32);
    for (char c : str) {
        if (c != '-') hex += c;
    }

    if (hex.size() != 32) {
        return {}; // null UUID on parse failure
    }

    auto parse_u64 = [](const std::string& s, size_t offset) -> uint64_t {
        uint64_t val = 0;
        for (size_t i = 0; i < 16; ++i) {
            char c = s[offset + i];
            uint8_t nibble;
            if (c >= '0' && c <= '9')      nibble = static_cast<uint8_t>(c - '0');
            else if (c >= 'a' && c <= 'f') nibble = static_cast<uint8_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') nibble = static_cast<uint8_t>(c - 'A' + 10);
            else return 0;
            val = (val << 4) | nibble;
        }
        return val;
    };

    return {parse_u64(hex, 0), parse_u64(hex, 16)};
}

std::string UUID::to_string() const {
    // Format: 8-4-4-4-12
    char buf[37];
    std::snprintf(buf, sizeof(buf),
        "%08x-%04x-%04x-%04x-%012llx",
        static_cast<uint32_t>(hi >> 32),
        static_cast<uint16_t>(hi >> 16),
        static_cast<uint16_t>(hi),
        static_cast<uint16_t>(lo >> 48),
        static_cast<unsigned long long>(lo & 0x0000FFFFFFFFFFFFULL));
    return std::string(buf);
}

// =============================================================================
// WhiteboardObject — Construction
// =============================================================================

WhiteboardObject::WhiteboardObject()
    : id_(UUID::generate()) {}

WhiteboardObject::WhiteboardObject(UUID id)
    : id_(id) {}

// =============================================================================
// Helper: enum ↔ string conversion
// =============================================================================

namespace {

const char* line_cap_to_string(LineCap c) {
    switch (c) {
        case LineCap::Butt:   return "butt";
        case LineCap::Round:  return "round";
        case LineCap::Square: return "square";
    }
    return "round";
}

LineCap string_to_line_cap(const std::string& s) {
    if (s == "butt")   return LineCap::Butt;
    if (s == "square") return LineCap::Square;
    return LineCap::Round;
}

const char* line_join_to_string(LineJoin j) {
    switch (j) {
        case LineJoin::Miter: return "miter";
        case LineJoin::Round: return "round";
        case LineJoin::Bevel: return "bevel";
    }
    return "round";
}

LineJoin string_to_line_join(const std::string& s) {
    if (s == "miter") return LineJoin::Miter;
    if (s == "bevel") return LineJoin::Bevel;
    return LineJoin::Round;
}

// Parse "#RRGGBB" or "#RRGGBBAA" hex string → RGBA.
RGBA parse_color_string(const std::string& s) {
    if (s.empty() || s[0] != '#') return RGBA::black();

    std::string hex = s.substr(1);
    uint32_t val = 0;
    for (char c : hex) {
        uint8_t nibble;
        if (c >= '0' && c <= '9')      nibble = static_cast<uint8_t>(c - '0');
        else if (c >= 'a' && c <= 'f') nibble = static_cast<uint8_t>(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') nibble = static_cast<uint8_t>(c - 'A' + 10);
        else return RGBA::black();
        val = (val << 4) | nibble;
    }

    if (hex.size() == 6) {
        return RGBA{
            static_cast<uint8_t>((val >> 16) & 0xFF),
            static_cast<uint8_t>((val >> 8)  & 0xFF),
            static_cast<uint8_t>( val        & 0xFF),
            255
        };
    }
    if (hex.size() == 8) {
        return RGBA{
            static_cast<uint8_t>((val >> 24) & 0xFF),
            static_cast<uint8_t>((val >> 16) & 0xFF),
            static_cast<uint8_t>((val >> 8)  & 0xFF),
            static_cast<uint8_t>( val        & 0xFF)
        };
    }
    return RGBA::black();
}

// Transform 6-element array ↔ JSON.
json affine_to_json(const AffineTransform& t) {
    return json::array({t.m[0], t.m[1], t.m[2], t.m[3], t.m[4], t.m[5]});
}

AffineTransform affine_from_json(const json& j) {
    if (!j.is_array() || j.size() != 6) return AffineTransform::identity();
    return AffineTransform(
        j[0].get<float>(), j[1].get<float>(), j[2].get<float>(),
        j[3].get<float>(), j[4].get<float>(), j[5].get<float>()
    );
}

} // anonymous namespace

// =============================================================================
// WhiteboardObject — Serialization
// =============================================================================

json WhiteboardObject::serialize() const {
    json j;

    // Type tag (written first for readability in the JSON file)
    j["type"] = type_name();

    // Identity
    j["id"] = id_.to_string();

    // Ordering & state
    j["z_order"] = z_order_;
    j["visible"] = visible_;
    j["locked"]  = locked_;

    // Styling
    j["stroke_color"] = stroke_color_.to_hex_string();
    j["fill_color"]   = fill_color_.to_hex_string();
    j["stroke_width"] = stroke_width_;
    j["line_cap"]     = line_cap_to_string(line_cap_);
    j["line_join"]    = line_join_to_string(line_join_);
    j["opacity"]      = opacity_;

    // Transform (omit if identity to keep JSON lean)
    if (!transform_.is_identity()) {
        j["transform"] = affine_to_json(transform_);
    }

    // Derived-class properties
    serialize_properties(j);

    return j;
}

void WhiteboardObject::deserialize_base(const json& j) {
    if (j.contains("id")) {
        id_ = UUID::from_string(j["id"].get<std::string>());
        if (id_.is_null()) id_ = UUID::generate();
    }

    if (j.contains("z_order"))      z_order_      = j["z_order"].get<int32_t>();
    if (j.contains("visible"))      visible_      = j["visible"].get<bool>();
    if (j.contains("locked"))       locked_       = j["locked"].get<bool>();
    if (j.contains("stroke_color")) stroke_color_ = parse_color_string(j["stroke_color"].get<std::string>());
    if (j.contains("fill_color"))   fill_color_   = parse_color_string(j["fill_color"].get<std::string>());
    if (j.contains("stroke_width")) stroke_width_ = j["stroke_width"].get<float>();
    if (j.contains("line_cap"))     line_cap_     = string_to_line_cap(j["line_cap"].get<std::string>());
    if (j.contains("line_join"))    line_join_    = string_to_line_join(j["line_join"].get<std::string>());
    if (j.contains("opacity"))      opacity_      = j["opacity"].get<float>();

    if (j.contains("transform")) {
        transform_ = affine_from_json(j["transform"]);
    } else {
        transform_ = AffineTransform::identity();
    }
}

// =============================================================================
// WhiteboardObject — Factory
// =============================================================================

std::unique_ptr<WhiteboardObject> WhiteboardObject::from_json(const json& j) {
    if (!j.contains("type") || !j["type"].is_string()) {
        return nullptr;
    }

    std::string type = j["type"].get<std::string>();

    std::unique_ptr<WhiteboardObject> obj;

    if (type == "freehand") {
        obj = std::make_unique<FreehandStroke>();
    } else if (type == "highlighter") {
        obj = std::make_unique<HighlighterStroke>();
    } else if (type == "line") {
        obj = std::make_unique<LineObject>();
    } else if (type == "rect") {
        obj = std::make_unique<RectObject>();
    } else if (type == "ellipse") {
        obj = std::make_unique<EllipseObject>();
    } else if (type == "circle") {
        obj = std::make_unique<CircleObject>();
    } else if (type == "arrow") {
        obj = std::make_unique<ArrowObject>();
    } else if (type == "polygon") {
        obj = std::make_unique<PolygonObject>();
    } else if (type == "triangle") {
        obj = std::make_unique<TriangleObject>();
    } else if (type == "shape") {
        obj = std::make_unique<ShapeObject>();
    } else if (type == "image") {
        obj = std::make_unique<ImageObject>();
    } else if (type == "ppt") {
        obj = std::make_unique<PptObject>();
    } else {
        return nullptr; // unknown type — skip gracefully
    }

    obj->deserialize_base(j);
    obj->deserialize_properties(j);

    return obj;
}

// =============================================================================
// WhiteboardObject — copy_base_from
// =============================================================================

void WhiteboardObject::copy_base_from(const WhiteboardObject& src) {
    // Note: id_ is NOT copied here — callers decide whether to keep or regen.
    z_order_      = src.z_order_;
    visible_      = src.visible_;
    locked_       = src.locked_;
    stroke_color_ = src.stroke_color_;
    fill_color_   = src.fill_color_;
    stroke_width_ = src.stroke_width_;
    line_cap_     = src.line_cap_;
    line_join_    = src.line_join_;
    opacity_      = src.opacity_;
    transform_    = src.transform_;
}

} // namespace lb