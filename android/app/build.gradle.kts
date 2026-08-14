plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.renderedideas.adpatcher"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.renderedideas.adpatcher"
        minSdk = 26        // MediaMuxer multi-track needs API 26
        targetSdk = 34
        versionCode = 2
        versionName = "0.1.1"
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    // FFT for the alignment cross-correlation
    implementation("com.github.wendykierp:JTransforms:3.1")
}
