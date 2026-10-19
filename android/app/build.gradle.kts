import java.util.Properties

val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { f -> f.exists() }
        ?.inputStream()?.use { s -> load(s) }
}

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.fuzzypyjamas.adpatcher"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.fuzzypyjamas.adpatcher"
        minSdk = 26        // MediaMuxer multi-track needs API 26
        targetSdk = 34
        versionCode = 7
        versionName = "0.4.1"
        // on-device VLM (libadpvlm, docs/plan/10) is arm64 only
        ndk { abiFilters += "arm64-v8a" }
        externalNativeBuild {
            cmake {
                arguments += listOf("-DANDROID_STL=c++_static")
                targets += "adpvlm"
            }
        }
    }

    // NDK/CMake location are machine-specific: local.properties keys
    // adp.ndkPath (NDK of ndkVersion) and cmake.dir (CMake 3.22+)
    ndkVersion = "27.2.12479018"
    localProps.getProperty("adp.ndkPath")?.let { path -> ndkPath = path }
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // debug-signed so the APK installs directly during development
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    buildFeatures {
        buildConfig = true   // version string used in the session log header
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    // JVM unit tests (AlignerParityTest): android.* calls (Log,
    // SystemClock) return defaults instead of throwing
    testOptions {
        unitTests.isReturnDefaultValues = true
        unitTests.all { test ->
            System.getProperty("parity.dir")?.let {
                test.systemProperty("parity.dir", it)
            }
            test.testLogging { showStandardStreams = true }
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    // FFT for the alignment cross-correlation
    implementation("com.github.wendykierp:JTransforms:3.1")
    testImplementation("junit:junit:4.13.2")
}
