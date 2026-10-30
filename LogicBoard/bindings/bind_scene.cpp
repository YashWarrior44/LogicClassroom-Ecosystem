// =============================================================================
// bind_scene.cpp — Expose the full scene graph to Python.
//
// Binds: Vec2f, RGBA, AABB, AffineTransform, UUID, WhiteboardObject (abstract),
//        FreehandStroke, HighlighterStroke, LineObject, RectObject,
//        EllipseObject, ArrowObject, PolygonObject, TriangleObject,
//        ImageObject, PptObject, Scene, Board.
// =============================================================================

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>
#include <pybind11/operators.h>

#include <scene/WhiteboardObject.h>
#include <scene/FreehandStroke.h>
#include <scene/HighlighterStroke.h>
#include <scene/LineObject.h>
#include <scene/RectObject.h>
#include <scene/EllipseObject.h>
#include <scene/CircleObject.h>
#include <scene/ArrowObject.h>
#include <scene/PolygonObject.h>
#include <scene/TriangleObject.h>
#include <scene/ShapeObject.h>
#include <scene/ImageObject.h>
#include <scene/PptObject.h>
#include <scene/Scene.h>
#include <math/Types.h>
#include <math/AffineTransform.h>

namespace py = pybind11;
using namespace lb;

// Helper: convert Python list of [x,y] pairs to std::vector<Vec2f>.
static std::vector<Vec2f> list_to_vec2f_vec(const py::list& lst) {
    std::vector<Vec2f> out;
    out.reserve(lst.size());
    for (const auto& item : lst) {
        auto pair = item.cast<py::sequence>();
        out.push_back({pair[0].cast<float>(), pair[1].cast<float>()});
    }
    return out;
}

// Helper: convert std::vector<Vec2f> to Python list of tuples.
static py::list vec2f_vec_to_list(const std::vector<Vec2f>& v) {
    py::list out;
    for (const auto& p : v) {
        out.append(py::make_tuple(p.x, p.y));
    }
    return out;
}

void bind_scene(py::module_& m) {

    // =========================================================================
    // Vec2f
    // =========================================================================
    py::class_<Vec2f>(m, "Vec2f")
        .def(py::init<>())
        .def(py::init<float, float>(), py::arg("x"), py::arg("y"))
        .def_readwrite("x", &Vec2f::x)
        .def_readwrite("y", &Vec2f::y)
        .def(py::self + py::self)
        .def(py::self - py::self)
        .def(py::self * float())
        .def(py::self / float())
        .def(py::self == py::self)
        .def(py::self != py::self)
        .def("dot",        &Vec2f::dot)
        .def("cross",      &Vec2f::cross)
        .def("length",     &Vec2f::length)
        .def("length_sq",  &Vec2f::length_sq)
        .def("normalized", &Vec2f::normalized)
        .def("perp",       &Vec2f::perp)
        .def("distance",   &Vec2f::distance)
        .def("distance_sq",&Vec2f::distance_sq)
        .def("__repr__", [](const Vec2f& v) {
            return "Vec2f(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ")";
        });

    // =========================================================================
    // RGBA
    // =========================================================================
    py::class_<RGBA>(m, "RGBA")
        .def(py::init<>())
        .def(py::init<uint8_t, uint8_t, uint8_t, uint8_t>(),
             py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a") = 255)
        .def_readwrite("r", &RGBA::r)
        .def_readwrite("g", &RGBA::g)
        .def_readwrite("b", &RGBA::b)
        .def_readwrite("a", &RGBA::a)
        .def_static("from_hex",    &RGBA::from_hex, py::arg("hex"), py::arg("alpha") = 255)
        .def("to_hex_string",      &RGBA::to_hex_string)
        .def_static("black",       &RGBA::black)
        .def_static("white",       &RGBA::white)
        .def_static("transparent", &RGBA::transparent)
        .def_static("blue",        &RGBA::blue)
        .def_static("red",         &RGBA::red)
        .def_static("green",       &RGBA::green)
        .def_static("orange",      &RGBA::orange)
        .def(py::self == py::self)
        .def(py::self != py::self)
        .def("__repr__", [](const RGBA& c) {
            return "RGBA(" + std::to_string(c.r) + ", " + std::to_string(c.g)
                   + ", " + std::to_string(c.b) + ", " + std::to_string(c.a) + ")";
        });

    // =========================================================================
    // AABB
    // =========================================================================
    py::class_<AABB>(m, "AABB")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f>(), py::arg("min_pt"), py::arg("max_pt"))
        .def_readwrite("min_pt", &AABB::min_pt)
        .def_readwrite("max_pt", &AABB::max_pt)
        .def("width",      &AABB::width)
        .def("height",     &AABB::height)
        .def("center",     &AABB::center)
        .def("size",       &AABB::size)
        .def("area",       &AABB::area)
        .def("contains",   &AABB::contains)
        .def("intersects", &AABB::intersects)
        .def("merged",     &AABB::merged)
        .def("expanded",   &AABB::expanded)
        .def("is_valid",   &AABB::is_valid)
        .def("__repr__", [](const AABB& b) {
            return "AABB((" + std::to_string(b.min_pt.x) + "," + std::to_string(b.min_pt.y)
                   + "), (" + std::to_string(b.max_pt.x) + "," + std::to_string(b.max_pt.y) + "))";
        });

    // =========================================================================
    // Enums
    // =========================================================================
    py::enum_<LineCap>(m, "LineCap")
        .value("Butt",   LineCap::Butt)
        .value("Round",  LineCap::Round)
        .value("Square", LineCap::Square);

    py::enum_<LineJoin>(m, "LineJoin")
        .value("Miter", LineJoin::Miter)
        .value("Round", LineJoin::Round)
        .value("Bevel", LineJoin::Bevel);

    // =========================================================================
    // AffineTransform
    // =========================================================================
    py::class_<AffineTransform>(m, "AffineTransform")
        .def(py::init<>())
        .def(py::init<float, float, float, float, float, float>(),
             py::arg("a"), py::arg("b"), py::arg("tx"),
             py::arg("c"), py::arg("d"), py::arg("ty"))
        .def("a",  &AffineTransform::a)
        .def("b",  &AffineTransform::b)
        .def("tx", &AffineTransform::tx)
        .def("c",  &AffineTransform::c)
        .def("d",  &AffineTransform::d)
        .def("ty", &AffineTransform::ty)
        .def_static("identity",     &AffineTransform::identity)
        .def_static("translation",  &AffineTransform::translation)
        .def_static("scaling",      &AffineTransform::scaling)
        .def_static("uniform_scale",&AffineTransform::uniform_scale)
        .def_static("rotation",     &AffineTransform::rotation)
        .def_static("rotation_around", &AffineTransform::rotation_around)
        .def("apply",  py::overload_cast<Vec2f>(&AffineTransform::apply, py::const_))
        .def("apply_direction", &AffineTransform::apply_direction)
        .def("then",       &AffineTransform::then)
        .def("translated", &AffineTransform::translated)
        .def("scaled",     &AffineTransform::scaled)
        .def("rotated",    &AffineTransform::rotated)
        .def("inverse",    &AffineTransform::inverse)
        .def("is_identity",&AffineTransform::is_identity)
        .def("apply_aabb", py::overload_cast<const AABB&>(&AffineTransform::apply, py::const_))
        .def(py::self == py::self)
        .def(py::self != py::self)
        .def("__repr__", [](const AffineTransform& t) {
            return "AffineTransform([" + std::to_string(t.a()) + "," + std::to_string(t.b())
                   + "," + std::to_string(t.tx()) + ", " + std::to_string(t.c())
                   + "," + std::to_string(t.d()) + "," + std::to_string(t.ty()) + "])";
        });

    // =========================================================================
    // UUID
    // =========================================================================
    py::class_<UUID>(m, "UUID")
        .def(py::init<>())
        .def_static("generate",    &UUID::generate)
        .def_static("from_string", &UUID::from_string)
        .def("to_string",          &UUID::to_string)
        .def("is_null",            &UUID::is_null)
        .def(py::self == py::self)
        .def(py::self != py::self)
        .def("__repr__", [](const UUID& u) {
            return "UUID('" + u.to_string() + "')";
        })
        .def("__str__", &UUID::to_string)
        .def("__hash__", [](const UUID& u) {
            return std::hash<uint64_t>()(u.hi) ^ (std::hash<uint64_t>()(u.lo) << 1);
        });

    // =========================================================================
    // WhiteboardObject (abstract base — not directly constructible from Python)
    // =========================================================================
    py::class_<WhiteboardObject>(m, "WhiteboardObject")
        .def_property("id",
            [](const WhiteboardObject& o) { return o.id(); },
            &WhiteboardObject::set_id)
        .def_property("z_order",      &WhiteboardObject::z_order,      &WhiteboardObject::set_z_order)
        .def_property("visible",      &WhiteboardObject::visible,      &WhiteboardObject::set_visible)
        .def_property("locked",       &WhiteboardObject::locked,       &WhiteboardObject::set_locked)
        .def_property("stroke_color", &WhiteboardObject::stroke_color, &WhiteboardObject::set_stroke_color)
        .def_property("fill_color",   &WhiteboardObject::fill_color,   &WhiteboardObject::set_fill_color)
        .def_property("stroke_width", &WhiteboardObject::stroke_width, &WhiteboardObject::set_stroke_width)
        .def_property("line_cap",     &WhiteboardObject::line_cap,     &WhiteboardObject::set_line_cap)
        .def_property("line_join",    &WhiteboardObject::line_join,    &WhiteboardObject::set_line_join)
        .def_property("opacity",      &WhiteboardObject::opacity,      &WhiteboardObject::set_opacity)
        .def_property("transform",    &WhiteboardObject::transform,    &WhiteboardObject::set_transform)
        .def("translate",             &WhiteboardObject::translate)
        .def("rotate",                &WhiteboardObject::rotate)
        .def("scale",                 &WhiteboardObject::scale)
        .def("local_bounding_box",    &WhiteboardObject::local_bounding_box)
        .def("bounding_box",          &WhiteboardObject::bounding_box)
        .def("hit_test_local",        &WhiteboardObject::hit_test_local)
        .def("hit_test",              &WhiteboardObject::hit_test)
        .def_property_readonly("type_name", &WhiteboardObject::type_name)
        .def("serialize",             &WhiteboardObject::serialize)
        .def("memory_estimate",       &WhiteboardObject::memory_estimate);

    // =========================================================================
    // FreehandStroke
    // =========================================================================
    py::class_<FreehandStroke, WhiteboardObject>(m, "FreehandStroke")
        .def(py::init<>())
        .def(py::init<UUID>())
        .def_property_readonly("point_count", &FreehandStroke::point_count)
        .def_property_readonly("empty",       &FreehandStroke::empty)
        .def("points", [](const FreehandStroke& s) { return vec2f_vec_to_list(s.points()); })
        .def("pressures", [](const FreehandStroke& s) { return s.pressures(); })
        .def("reserve",   &FreehandStroke::reserve)
        .def("add_point", &FreehandStroke::add_point,
             py::arg("pt"), py::arg("pressure") = -1.0f)
        .def("set_points", [](FreehandStroke& s, const py::list& pts) {
            s.set_points(list_to_vec2f_vec(pts));
        })
        .def("set_points_with_pressures", [](FreehandStroke& s, const py::list& pts, const std::vector<float>& prs) {
            s.set_points(list_to_vec2f_vec(pts), prs);
        })
        .def("pop_back",  &FreehandStroke::pop_back)
        .def("clear",     &FreehandStroke::clear)
        .def("simplify",  &FreehandStroke::simplify, py::arg("epsilon") = 1.0f);

    // =========================================================================
    // HighlighterStroke
    // =========================================================================
    py::class_<HighlighterStroke, WhiteboardObject>(m, "HighlighterStroke")
        .def(py::init<>())
        .def(py::init<UUID>())
        .def_property("height", &HighlighterStroke::height, &HighlighterStroke::set_height)
        .def_property_readonly("point_count", &HighlighterStroke::point_count)
        .def_property_readonly("empty",       &HighlighterStroke::empty)
        .def("points", [](const HighlighterStroke& s) { return vec2f_vec_to_list(s.points()); })
        .def("reserve",   &HighlighterStroke::reserve)
        .def("add_point", &HighlighterStroke::add_point)
        .def("set_points", [](HighlighterStroke& s, const py::list& pts) {
            s.set_points(list_to_vec2f_vec(pts));
        })
        .def("pop_back",  &HighlighterStroke::pop_back)
        .def("clear",     &HighlighterStroke::clear)
        .def("simplify",  &HighlighterStroke::simplify, py::arg("epsilon") = 1.5f);

    // =========================================================================
    // LineObject
    // =========================================================================
    py::class_<LineObject, WhiteboardObject>(m, "LineObject")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f>())
        .def(py::init<Vec2f, Vec2f, UUID>())
        .def_property("start",  &LineObject::start,  &LineObject::set_start)
        .def_property("end",    &LineObject::end,    &LineObject::set_end)
        .def_property_readonly("length",    &LineObject::length)
        .def_property_readonly("direction", &LineObject::direction)
        .def_property_readonly("midpoint",  &LineObject::midpoint);

    // =========================================================================
    // RectObject
    // =========================================================================
    py::class_<RectObject, WhiteboardObject>(m, "RectObject")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f>())
        .def(py::init<Vec2f, Vec2f, UUID>())
        .def_property("origin",        &RectObject::origin,        &RectObject::set_origin)
        .def_property("size",          &RectObject::size,          &RectObject::set_size)
        .def_property("corner_radius", &RectObject::corner_radius, &RectObject::set_corner_radius)
        .def_property_readonly("width",  &RectObject::width)
        .def_property_readonly("height", &RectObject::height)
        .def_property_readonly("center", &RectObject::center)
        .def_static("from_center",  &RectObject::from_center)
        .def_static("from_corners", &RectObject::from_corners);

    // =========================================================================
    // EllipseObject
    // =========================================================================
    py::class_<EllipseObject, WhiteboardObject>(m, "EllipseObject")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f>())
        .def(py::init<Vec2f, Vec2f, UUID>())
        .def_property("center", &EllipseObject::center, &EllipseObject::set_center)
        .def_property("radii",  &EllipseObject::radii,  &EllipseObject::set_radii)
        .def_property_readonly("rx",        &EllipseObject::rx)
        .def_property_readonly("ry",        &EllipseObject::ry)
        .def_property_readonly("is_circle", &EllipseObject::is_circle)
        .def_static("circle",    &EllipseObject::circle)
        .def_static("from_rect", &EllipseObject::from_rect);

    // =========================================================================
    // CircleObject (true circle: center + single radius)
    // =========================================================================
    py::class_<CircleObject, WhiteboardObject>(m, "CircleObject")
        .def(py::init<>())
        .def(py::init<Vec2f, float>())
        .def(py::init<Vec2f, float, UUID>())
        .def_property("center", &CircleObject::center, &CircleObject::set_center)
        .def_property("radius", &CircleObject::radius, &CircleObject::set_radius)
        .def("scale_uniform",   &CircleObject::scale_uniform)
        .def_static("from_rect", &CircleObject::from_rect);

    // =========================================================================
    // ArrowObject
    // =========================================================================
    py::class_<ArrowObject, WhiteboardObject>(m, "ArrowObject")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f>())
        .def(py::init<Vec2f, Vec2f, UUID>())
        .def_property("start",        &ArrowObject::start,        &ArrowObject::set_start)
        .def_property("end",          &ArrowObject::end,          &ArrowObject::set_end)
        .def_property("head_length",  &ArrowObject::head_length,  &ArrowObject::set_head_length)
        .def_property("head_width",   &ArrowObject::head_width,   &ArrowObject::set_head_width)
        .def_property("head_at_start",&ArrowObject::head_at_start,&ArrowObject::set_head_at_start)
        .def_property("head_at_end",  &ArrowObject::head_at_end,  &ArrowObject::set_head_at_end)
        .def_property_readonly("length",    &ArrowObject::length)
        .def_property_readonly("direction", &ArrowObject::direction)
        .def_property_readonly("midpoint",  &ArrowObject::midpoint)
        .def("end_head_triangle", [](const ArrowObject& a) {
            auto tri = a.end_head_triangle();
            return py::make_tuple(
                py::make_tuple(tri[0].x, tri[0].y),
                py::make_tuple(tri[1].x, tri[1].y),
                py::make_tuple(tri[2].x, tri[2].y));
        })
        .def("start_head_triangle", [](const ArrowObject& a) {
            auto tri = a.start_head_triangle();
            return py::make_tuple(
                py::make_tuple(tri[0].x, tri[0].y),
                py::make_tuple(tri[1].x, tri[1].y),
                py::make_tuple(tri[2].x, tri[2].y));
        });

    // =========================================================================
    // PolygonObject
    // =========================================================================
    py::class_<PolygonObject, WhiteboardObject>(m, "PolygonObject")
        .def(py::init<>())
        .def(py::init([](const py::list& verts, bool closed) {
            return new PolygonObject(list_to_vec2f_vec(verts), closed);
        }), py::arg("vertices"), py::arg("closed") = true)
        .def_property("closed", &PolygonObject::closed, &PolygonObject::set_closed)
        .def_property_readonly("vertex_count", &PolygonObject::vertex_count)
        .def_property_readonly("empty",        &PolygonObject::empty)
        .def("vertices", [](const PolygonObject& p) { return vec2f_vec_to_list(p.vertices()); })
        .def("set_vertices", [](PolygonObject& p, const py::list& verts) {
            p.set_vertices(list_to_vec2f_vec(verts));
        })
        .def("vertex",        &PolygonObject::vertex)
        .def("set_vertex",    &PolygonObject::set_vertex)
        .def("add_vertex",    &PolygonObject::add_vertex)
        .def("insert_vertex", &PolygonObject::insert_vertex)
        .def("remove_vertex", &PolygonObject::remove_vertex)
        .def("signed_area",   &PolygonObject::signed_area)
        .def("centroid",      &PolygonObject::centroid)
        .def("perimeter",     &PolygonObject::perimeter);

    // =========================================================================
    // TriangleObject — first-class three-vertex triangle
    // =========================================================================
    py::class_<TriangleObject, WhiteboardObject>(m, "TriangleObject")
        .def(py::init<>())
        .def(py::init<Vec2f, Vec2f, Vec2f>(),
             py::arg("p1"), py::arg("p2"), py::arg("p3"))
        .def(py::init<Vec2f, Vec2f, Vec2f, UUID>(),
             py::arg("p1"), py::arg("p2"), py::arg("p3"), py::arg("id"))
        .def_property("p1", &TriangleObject::p1, &TriangleObject::set_p1)
        .def_property("p2", &TriangleObject::p2, &TriangleObject::set_p2)
        .def_property("p3", &TriangleObject::p3, &TriangleObject::set_p3)
        .def("set_vertices", &TriangleObject::set_vertices,
             py::arg("a"), py::arg("b"), py::arg("c"))
        .def_property_readonly("centroid",    &TriangleObject::centroid)
        .def_property_readonly("signed_area", &TriangleObject::signed_area)
        .def_property_readonly("perimeter",   &TriangleObject::perimeter)
        .def_static("from_drag_rect", &TriangleObject::from_drag_rect,
                    py::arg("a"), py::arg("b"));

    // =========================================================================
    // ShapeObject — every shape kind beyond the original six (see
    // ui/canvas/shape_geometry.py). The kind is a plain string so a new shape
    // needs no engine change; unknown kinds simply draw nothing.
    // =========================================================================
    py::class_<ShapeObject, WhiteboardObject>(m, "ShapeObject")
        .def(py::init<>())
        .def(py::init<std::string, Vec2f, Vec2f>(),
             py::arg("kind"), py::arg("start"), py::arg("end"))
        .def(py::init<std::string, Vec2f, Vec2f, UUID>(),
             py::arg("kind"), py::arg("start"), py::arg("end"), py::arg("id"))
        .def_property("kind",  &ShapeObject::kind,  &ShapeObject::set_kind)
        .def_property("start", &ShapeObject::start, &ShapeObject::set_start)
        .def_property("end",   &ShapeObject::end,   &ShapeObject::set_end)
        .def_property_readonly("is_line_kind", &ShapeObject::is_line_kind);

    // =========================================================================
    // ImageObject
    // =========================================================================
    py::class_<ImageObject, WhiteboardObject>(m, "ImageObject")
        .def(py::init<>())
        .def(py::init<const std::string&, Vec2f, Vec2f>(),
             py::arg("src"), py::arg("origin"), py::arg("size"))
        .def(py::init<const std::string&, Vec2f, Vec2f, UUID>(),
             py::arg("src"), py::arg("origin"), py::arg("size"), py::arg("id"))
        .def_property("src",          &ImageObject::src,          &ImageObject::set_src)
        .def_property("origin",       &ImageObject::origin,       &ImageObject::set_origin)
        .def_property("size",         &ImageObject::size,         &ImageObject::set_size)
        .def_property("natural_size", &ImageObject::natural_size, &ImageObject::set_natural_size)
        .def_property_readonly("width",  &ImageObject::width)
        .def_property_readonly("height", &ImageObject::height)
        .def_property_readonly("center", &ImageObject::center);

    // =========================================================================
    // PptObject
    // =========================================================================
    py::class_<PptObject, WhiteboardObject>(m, "PptObject")
        .def(py::init<>())
        .def(py::init<const std::string&, Vec2f, Vec2f>(),
             py::arg("src"), py::arg("origin"), py::arg("size"))
        .def(py::init<const std::string&, Vec2f, Vec2f, UUID>(),
             py::arg("src"), py::arg("origin"), py::arg("size"), py::arg("id"))
        .def_property("src",           &PptObject::src,           &PptObject::set_src)
        .def_property("origin",        &PptObject::origin,        &PptObject::set_origin)
        .def_property("size",          &PptObject::size,          &PptObject::set_size)
        .def_property("slide_count",   &PptObject::slide_count,   &PptObject::set_slide_count)
        .def_property("current_slide", &PptObject::current_slide, &PptObject::set_current_slide)
        .def_property("anim_step",     &PptObject::anim_step,     &PptObject::set_anim_step)
        .def_property("anim_counts",   &PptObject::anim_counts,   &PptObject::set_anim_counts)
        .def_property_readonly("anim_count_for_current", &PptObject::anim_count_for_current)
        .def_property_readonly("width",  &PptObject::width)
        .def_property_readonly("height", &PptObject::height)
        .def_property_readonly("center", &PptObject::center)
        .def("advance_animation", &PptObject::advance_animation)
        .def("retreat_animation", &PptObject::retreat_animation)
        .def("next_slide",        &PptObject::next_slide)
        .def("prev_slide",        &PptObject::prev_slide);

    // =========================================================================
    // Scene
    // =========================================================================
    py::class_<Scene>(m, "Scene")
        .def(py::init<>())
        .def(py::init<int32_t, int32_t>(), py::arg("width"), py::arg("height"))
        .def_property("canvas_width",  &Scene::canvas_width,
            [](Scene& s, int32_t w) { s.set_canvas_size(w, s.canvas_height()); })
        .def_property("canvas_height", &Scene::canvas_height,
            [](Scene& s, int32_t h) { s.set_canvas_size(s.canvas_width(), h); })
        .def("set_canvas_size",        &Scene::set_canvas_size)
        .def_property("background_color", &Scene::background_color, &Scene::set_background_color)
        .def_property("bg_image", &Scene::bg_image,
            [](Scene& s, const std::string& v) { s.set_bg_image(v); })
        .def_property_readonly("has_bg_image", &Scene::has_bg_image)
        .def("clear_bg_image", &Scene::clear_bg_image)
        .def_property_readonly("object_count", &Scene::object_count)
        .def_property_readonly("empty",        &Scene::empty)

        // Access
        // ----------------------------------------------------------------
        // return_value_policy::reference_internal ties the returned
        // pointer's lifetime to the parent Scene: while Python holds the
        // WhiteboardObject*, the Scene is guaranteed alive. Was previously
        // ``reference`` (no keep-alive), which meant a natural Python
        // pattern like ``obj = scene.find(id); ...; scene.clear()`` would
        // leave ``obj`` dangling and trigger a use-after-free on any
        // subsequent attribute access. reference_internal doesn't
        // eliminate all footguns — deliberately removing THIS object still
        // dangles this pointer — but it removes the whole class of
        // "parent destroyed under the child" crashes.
        .def("object_at",  &Scene::object_at,     py::return_value_policy::reference_internal)
        .def("find",        py::overload_cast<const UUID&>(&Scene::find, py::const_),
             py::return_value_policy::reference_internal)
        .def("find_mut",    py::overload_cast<const UUID&>(&Scene::find_mut),
             py::return_value_policy::reference_internal)
        .def("find_index",  &Scene::find_index)
        .def("all_objects", &Scene::all_objects, py::return_value_policy::reference_internal)

        // Add/Remove — Python passes ownership via unique_ptr
        .def("add", [](Scene& s, WhiteboardObject* obj) -> WhiteboardObject* {
            // This overload is for when Python creates an object and wants
            // to add it.  pybind11 transfers ownership via unique_ptr.
            (void)obj;
            throw std::runtime_error("Use add_object() with a concrete type constructor");
            return nullptr;
        })
        .def("add_freehand", [](Scene& s, FreehandStroke* stroke) -> WhiteboardObject* {
            // Null-check: passing None from Python would land here as a
            // nullptr, and the previous ``stroke->clone_exact()`` would
            // segfault. Return nullptr (i.e. Python None) instead — the
            // correct "no-op, nothing added" behaviour.
            if (!stroke) return nullptr;
            auto clone = stroke->clone_exact();
            return s.add(std::move(clone));
        }, py::return_value_policy::reference_internal)
        .def("add_object", [](Scene& s, const py::object& py_obj) -> WhiteboardObject* {
            // Same defensive null-check for the generic add path. Both
            // ``None`` and a cast that yields nullptr are treated as
            // no-ops instead of crashing on the deref that follows.
            if (py_obj.is_none()) return nullptr;
            auto* raw = py_obj.cast<WhiteboardObject*>();
            if (!raw) return nullptr;
            auto clone = raw->clone_exact();
            return s.add(std::move(clone));
        }, py::return_value_policy::reference_internal)
        .def("remove", py::overload_cast<const UUID&>(&Scene::remove))
        .def("remove_at", &Scene::remove_at)
        .def("clear",     &Scene::clear)

        // Z-order
        .def("bring_to_front", &Scene::bring_to_front)
        .def("send_to_back",   &Scene::send_to_back)
        .def("bring_forward",  &Scene::bring_forward)
        .def("send_backward",  &Scene::send_backward)
        .def("normalize_z_order", &Scene::normalize_z_order)

        // Spatial queries — same keep-alive story as ``find`` above:
        // pointers returned here live as long as their parent Scene does.
        .def("hit_test",  &Scene::hit_test,
             py::arg("world_point"), py::arg("tolerance") = 4.0f,
             py::return_value_policy::reference_internal)
        .def("hit_test_all", &Scene::hit_test_all,
             py::arg("world_point"), py::arg("tolerance") = 4.0f,
             py::return_value_policy::reference_internal)
        .def("query_rect",           &Scene::query_rect,           py::return_value_policy::reference_internal)
        .def("query_rect_contained", &Scene::query_rect_contained, py::return_value_policy::reference_internal)

        // Serialization
        .def("serialize_to_string",    &Scene::serialize_to_string, py::arg("indent") = 2)
        .def("deserialize_from_string",&Scene::deserialize_from_string)
        .def("save_to_file",           &Scene::save_to_file, py::arg("path"), py::arg("indent") = 2)
        .def("load_from_file",         &Scene::load_from_file)

        // Deep copy
        .def("deep_copy",       &Scene::deep_copy)
        .def("deep_copy_exact", &Scene::deep_copy_exact)

        // Dirty tracking
        .def_property_readonly("is_dirty", &Scene::is_dirty)
        .def("mark_dirty",                 &Scene::mark_dirty)
        .def("clear_dirty",               &Scene::clear_dirty);

    // =========================================================================
    // Board
    // =========================================================================
    py::class_<Board>(m, "Board")
        .def(py::init<>())
        .def(py::init<const std::string&>(), py::arg("name"))
        .def_property("name",      &Board::name,      &Board::set_name)
        .def_property("theme",     &Board::theme,     &Board::set_theme)
        .def_property("last_page", &Board::last_page, &Board::set_last_page)
        .def_property_readonly("page_count", &Board::page_count)
        .def_property_readonly("empty",      &Board::empty)
        .def("page",     &Board::page,     py::return_value_policy::reference_internal)
        .def("page_mut", &Board::page_mut, py::return_value_policy::reference_internal)
        .def("add_page", py::overload_cast<>(&Board::add_page),
             py::return_value_policy::reference_internal)
        .def("add_page_at", &Board::add_page_at, py::arg("index"),
             py::return_value_policy::reference_internal)
        .def("remove_page", &Board::remove_page)
        .def("swap_pages",  &Board::swap_pages)
        .def("serialize_to_string",    &Board::serialize_to_string, py::arg("indent") = 2)
        .def("deserialize_from_string",&Board::deserialize_from_string)
        .def("save_to_file",           &Board::save_to_file, py::arg("path"), py::arg("indent") = 2)
        .def("load_from_file",         &Board::load_from_file);

    // =========================================================================
    // Module-level factory (deserialize any object from JSON string)
    // =========================================================================
    m.def("object_from_json_string", [](const std::string& json_str) {
        auto j = nlohmann::json::parse(json_str);
        return WhiteboardObject::from_json(j);
    }, py::arg("json_str"),
       "Deserialize a WhiteboardObject from a JSON string.");
}