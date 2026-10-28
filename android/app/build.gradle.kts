plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.roomscan.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.roomscan.app"
        minSdk = 29
        targetSdk = 34
        // CI passes the repo's VERSION and commit count, like the iPhone app
        versionCode = (project.findProperty("appBuild") as String? ?: "1").toInt()
        versionName = project.findProperty("appVersion") as String? ?: "0.0.0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
