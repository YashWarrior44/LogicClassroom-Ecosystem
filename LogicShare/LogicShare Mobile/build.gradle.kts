// Versions are pinned to what this machine's toolchain supports: AGP 8.13 and
// Kotlin 2.2.20 are in the local Gradle cache, and compileSdk 36 is the newest
// platform installed. Newer AndroidX releases demand a newer SDK and plugin,
// so the libraries in app/build.gradle.kts are pinned to the same era.
plugins {
    id("com.android.application") version "8.13.1" apply false
    id("org.jetbrains.kotlin.android") version "2.2.20" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.20" apply false
}

// The project lives under "Source Code📜". Building works from there, but the
// unit-test JVM does not even start: its classpath points into build/, the
// JDK reads classpath entries as URLs, and it fails decoding the emoji
// ("Error decoding percent encoded characters"). Going through an ASCII
// junction does not help — Gradle resolves the real path. So when the
// checkout path is not plain ASCII, build output goes to an ASCII folder
// instead. Anywhere else, build/ stays where Android Studio expects it.
// export_binaries.py computes the same location to collect the APK.
// toRealPath, not canonicalPath: only the former sees through a junction.
val asciiPath = rootDir.toPath().toRealPath().toString().all { it.code < 128 }
if (!asciiPath) {
    val base = File(System.getenv("LOCALAPPDATA") ?: System.getProperty("java.io.tmpdir"),
        "LogicShareBuild")
    allprojects { layout.buildDirectory.set(File(base, project.name)) }
}
