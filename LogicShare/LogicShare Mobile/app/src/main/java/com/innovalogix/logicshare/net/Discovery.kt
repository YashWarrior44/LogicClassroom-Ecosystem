package com.innovalogix.logicshare.net

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import com.innovalogix.logicshare.core.AppLog
import java.net.Inet4Address
import java.util.ArrayDeque

/** A board as discovery reports it — the four things Windows' listener
 *  emits: class, section, room, ip. */
data class FoundBoard(val cls: String, val section: String, val room: String, val ip: String)

/**
 * mDNS discovery of ``_logicboard._tcp`` — Windows' Zeroconf ServiceBrowser.
 *
 * Behaviour matches the Windows listener deliberately:
 *   - a found service is resolved (with retries) and its TXT record read
 *     for class / section / room;
 *   - REMOVALS ARE IGNORED, as LogicBoardListener.remove_service does: a
 *     board leaves the list only when a send finds it unreachable, on
 *     Refresh, or when the network changes. mDNS goodbyes are unreliable on
 *     school Wi-Fi, and a card vanishing while a teacher reaches for it is
 *     worse than one that turns out to be offline when tapped.
 *
 * Two Android specifics:
 *   - a MulticastLock is held while browsing. Without it many phones' Wi-Fi
 *     drivers filter multicast to save power and the browse finds nothing.
 *   - resolves are serialised. Before Android 14 NsdManager allows one
 *     resolve at a time and fails the rest with FAILURE_ALREADY_ACTIVE.
 */
class Discovery(ctx: Context, private val onFound: (FoundBoard) -> Unit) {
    private val app = ctx.applicationContext
    private val nsd = app.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var listener: NsdManager.DiscoveryListener? = null
    private var lock: WifiManager.MulticastLock? = null

    private val retry = android.os.Handler(android.os.Looper.getMainLooper())
    private val queue = ArrayDeque<Pair<NsdServiceInfo, Int>>()
    private var resolving = false
    @Volatile private var generation = 0

    /** True on success. A failure here is Windows' "discovery could not
     *  start" — the caller enters the disabled state. */
    @Synchronized
    fun start(): Boolean {
        stop()
        val gen = ++generation
        try {
            val wifi = app.getSystemService(Context.WIFI_SERVICE) as WifiManager
            lock = wifi.createMulticastLock("LogicShare:mdns").apply {
                setReferenceCounted(false); acquire()
            }
        } catch (e: Exception) {
            AppLog.w("Multicast lock unavailable: ${e.message}")
        }
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                AppLog.d("mDNS browse started for $serviceType")
            }
            override fun onServiceFound(info: NsdServiceInfo) {
                if (gen != generation) return
                enqueue(info, gen)
            }
            override fun onServiceLost(info: NsdServiceInfo) {
                // Deliberately ignored — see the class comment.
            }
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                AppLog.e("mDNS browse failed to start (error $errorCode)")
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }
        return try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
            listener = l
            true
        } catch (e: Exception) {
            AppLog.e("Failed to start device discovery (mDNS)", e)
            releaseLock()
            false
        }
    }

    @Synchronized
    fun stop() {
        generation++
        listener?.let { runCatching { nsd.stopServiceDiscovery(it) } }
        listener = null
        synchronized(queue) { queue.clear(); resolving = false }
        releaseLock()
    }

    private fun releaseLock() {
        runCatching { lock?.release() }
        lock = null
    }

    private fun enqueue(info: NsdServiceInfo, gen: Int) {
        synchronized(queue) {
            queue.addLast(info to gen)
            if (resolving) return
            resolving = true
        }
        next()
    }

    private fun next() {
        val item: Pair<NsdServiceInfo, Int>
        synchronized(queue) {
            if (queue.isEmpty()) { resolving = false; return }
            item = queue.removeFirst()
        }
        resolve(item.first, item.second, attempt = 1)
    }

    @Suppress("DEPRECATION")
    private fun resolve(info: NsdServiceInfo, gen: Int, attempt: Int) {
        if (gen != generation) { next(); return }
        try {
            nsd.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(si: NsdServiceInfo, errorCode: Int) {
                    // Windows retries get_service_info five times, 0.4 s apart.
                    if (attempt < 5 && gen == generation) {
                        // Scheduled, not slept: this runs on NsdManager's
                        // callback thread, and sleeping there would stall
                        // every other discovery callback with it.
                        retry.postDelayed({ resolve(info, gen, attempt + 1) }, 400)
                    } else {
                        AppLog.w("No TXT properties for service: ${info.serviceName} (error $errorCode)")
                        next()
                    }
                }

                override fun onServiceResolved(si: NsdServiceInfo) {
                    try {
                        if (gen == generation) report(si)
                    } finally {
                        next()
                    }
                }
            })
        } catch (e: Exception) {
            AppLog.w("Resolve failed for ${info.serviceName}: ${e.message}")
            next()
        }
    }

    @Suppress("DEPRECATION")
    private fun report(si: NsdServiceInfo) {
        val attrs = si.attributes ?: emptyMap()
        if (attrs.isEmpty()) {
            AppLog.w("No TXT properties for service: ${si.serviceName}")
            return
        }
        fun txt(k: String) = attrs[k]?.toString(Charsets.UTF_8).orEmpty()
        // IPv4 only: boards advertise their LAN IPv4 address, and every URL
        // the app builds is http://<ipv4>:16384.
        val ip = if (Build.VERSION.SDK_INT >= 34)
            si.hostAddresses.firstOrNull { it is Inet4Address }?.hostAddress
        else (si.host as? Inet4Address)?.hostAddress
        if (ip.isNullOrEmpty()) {
            AppLog.w("Service ${si.serviceName} resolved without an IPv4 address")
            return
        }
        val b = FoundBoard(txt("class"), txt("section"), txt("room"), ip)
        AppLog.i("Discovered board: class=${b.cls} section=${b.section} room=${b.room} ip=${b.ip}")
        onFound(b)
    }

    companion object {
        const val SERVICE_TYPE = "_logicboard._tcp."
    }
}
