plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    // The same application id the Flutter build shipped under. It is the
    // app's identity on every phone that already has LogicShare: change it and
    // this installs as a second, separate app instead of updating the first.
    namespace = "com.innovalogix.logicshare"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.innovalogix.logicshare"
        minSdk = 24
        targetSdk = 36
        // Both written by sync_versions.py from versions.py — edit there.
        // versionCode must only ever go UP: Android refuses to install an
        // update whose code is not higher than the installed one. The Flutter
        // V1.1.1 build was 3.
        versionCode = 6
        versionName = "1.1.3"
    }

    signingConfigs {
        // The Flutter release was signed with the debug key
        // (signingConfig signingConfigs.debug), so that key IS LogicShare's
        // identity on teachers' phones. Signing with anything else would make
        // Android refuse the update and demand an uninstall first.
        getByName("debug")
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }

    // The protocol and tracker tests run on the plain JVM. They log through
    // android.util.Log, which is a stub there; returning defaults lets that
    // be a no-op instead of an exception.
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

kotlin {
    // A bytecode target, not jvmToolchain(17): a toolchain would demand a
    // separately installed JDK 17, and the build runs on Android Studio's
    // bundled JDK, which is newer and emits 17 just as well.
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.09.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.core:core-ktx:1.17.0")
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
