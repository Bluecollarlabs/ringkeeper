plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Ingest endpoint: server/ingest.py (127.0.0.1:8092 on your PC) exposed on your tailnet
// via `tailscale serve --https=8444` (HTTPS, tailnet-only; see server/README.md).
// Set at build time with -PingestUrl=... or env RINGKEEPER_INGEST_URL, or at runtime in the
// app's Setup section. The default below is a placeholder.
// The bearer token is entered on the phone (never in the APK).
val ingestUrl: String = (project.findProperty("ingestUrl") as String?)
    ?: System.getenv("RINGKEEPER_INGEST_URL")
    ?: "https://your-server.your-tailnet.ts.net:8444/v1/events"

android {
    namespace = "org.bluecollarlabs.ringkeeper.app"
    compileSdk = 35
    ndkVersion = "27.3.13750724"

    defaultConfig {
        applicationId = "org.bluecollarlabs.ringkeeper.app"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        ndk { abiFilters += listOf("arm64-v8a") }
        buildConfigField("String", "INGEST_URL", "\"$ingestUrl\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        // the Rust core is prebuilt by core/build-android.sh into src/main/jniLibs
        jniLibs.useLegacyPackaging = false
    }
}

// Rust core (.so + UniFFI Kotlin bindings) is generated, not committed.
// Skip with -PskipRust when the outputs are already present.
val buildRustCore by tasks.registering(Exec::class) {
    group = "build"
    description = "cargo-ndk build of core/ + Kotlin bindings"
    workingDir = rootProject.file("../core")
    commandLine("bash", "build-android.sh", "release")
    onlyIf { !project.hasProperty("skipRust") }
}
tasks.named("preBuild") { dependsOn(buildRustCore) }

dependencies {
    // UniFFI Kotlin bindings runtime
    implementation("net.java.dev.jna:jna:5.17.0@aar")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.1")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.1")
    implementation("androidx.work:work-runtime-ktx:2.10.2")

    implementation(platform("androidx.compose:compose-bom:2025.06.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
