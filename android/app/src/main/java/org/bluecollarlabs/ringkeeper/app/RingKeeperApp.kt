package org.bluecollarlabs.ringkeeper.app

import android.app.Application
import org.bluecollarlabs.ringkeeper.app.sync.SyncWorker

class RingKeeperApp : Application() {
    override fun onCreate() {
        super.onCreate()
        SyncWorker.schedulePeriodic(this)
    }
}
