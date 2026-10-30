package com.innovalogix.logicshare.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import com.innovalogix.logicshare.core.AppLog
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Know WHICH network we are on, and know the moment it changes — the Android
 * half of net_watch.py.
 *
 * Identity, not presence: walking from the corridor SSID to the classroom one
 * keeps "online" true the whole way across, so a presence check sees nothing,
 * and every board discovered on the old network stays listed, pointing at an
 * address this phone can no longer reach. The fingerprint is the sorted set
 * of LAN IPv4 addresses (Wi-Fi / Ethernet only — never cellular, because a
 * board is only ever reachable over the classroom LAN), so a hop changes it.
 *
 * Immediacy: ConnectivityManager calls back the instant a network appears,
 * disappears or changes its addresses — Android's equivalent of the
 * iphlpapi!NotifyAddrChange the Windows app parks a thread in. The 3-second
 * poll in the main state machine remains as the backstop, exactly as on
 * Windows.
 */
class NetWatch(ctx: Context) {
    private val cm = ctx.applicationContext.getSystemService(Context.CONNECTIVITY_SERVICE)
            as ConnectivityManager
    private var callback: ConnectivityManager.NetworkCallback? = null

    /** "" when offline. */
    fun fingerprint(): String {
        val ips = sortedSetOf<String>()
        var cmAnswered = false
        try {
            // allNetworks is deprecated for apps that should track networks by
            // callback — which this does too (start()). The snapshot is still the
            // simplest correct answer to "what LAN addresses do I hold right now".
            @Suppress("DEPRECATION") val nets = cm.allNetworks
            for (net in nets) {
                val caps = cm.getNetworkCapabilities(net) ?: continue
                if (!isLan(caps)) continue
                val lp: LinkProperties = cm.getLinkProperties(net) ?: continue
                for (la in lp.linkAddresses) {
                    val a = la.address
                    if (a is Inet4Address && usable(a.hostAddress.orEmpty()))
                        ips += "${lp.interfaceName}:${a.hostAddress}"
                }
            }
            cmAnswered = true
        } catch (e: Exception) {
            AppLog.w("ConnectivityManager unavailable; falling back to interfaces: ${e.message}")
        }
        // The raw-interface fallback ONLY when ConnectivityManager could not
        // answer — never when it answered "no Wi-Fi". Measured on a Galaxy
        // M21: for several seconds after Wi-Fi is switched off, wlan0 still
        // lists its old address, so falling back on an empty answer made
        // alternate reads say offline / online / offline. Every change was
        // handled twice and "No Wi-Fi" took ~15 s to appear instead of being
        // immediate.
        if (!cmAnswered) ips += interfaceFallback()
        return ips.joinToString(",")
    }

    fun isOnline(fp: String = fingerprint()): Boolean = fp.isNotEmpty()

    /** Our own LAN IPv4 address, for the log and the about screen. */
    fun primaryAddress(): String =
        fingerprint().split(",").firstOrNull()?.substringAfter(':').orEmpty()

    /** Calls [onEvent] on a binder thread whenever a LAN network appears,
     *  goes, or changes its addresses. The caller re-reads the fingerprint
     *  and decides; this only says "look now". */
    fun start(onEvent: (String) -> Unit) {
        if (callback != null) return
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = onEvent("available")
            override fun onLost(network: Network) = onEvent("lost")
            override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) =
                onEvent("link-changed")
        }
        try {
            val req = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .addTransportType(NetworkCapabilities.TRANSPORT_ETHERNET)
                .build()
            cm.registerNetworkCallback(req, cb)
            callback = cb
        } catch (e: Exception) {
            AppLog.w("Network callback unavailable — relying on the poll: ${e.message}")
        }
    }

    fun stop() {
        callback?.let { runCatching { cm.unregisterNetworkCallback(it) } }
        callback = null
    }

    private fun isLan(caps: NetworkCapabilities): Boolean =
        caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)

    /** Same rule as net_watch._usable: 169.254.x.x means DHCP failed, and
     *  counting it would make a network that never came up look online. */
    private fun usable(ip: String) =
        ip.isNotEmpty() && !ip.startsWith("127.") && !ip.startsWith("169.254.")

    private fun interfaceFallback(): List<String> = try {
        NetworkInterface.getNetworkInterfaces().toList().flatMap { ni ->
            val n = ni.name.lowercase()
            val cellular = n.startsWith("rmnet") || n.startsWith("pdp") ||
                n.contains("ccmni") || n.contains("seth")
            if (!ni.isUp || ni.isLoopback || cellular) emptyList()
            else ni.inetAddresses.toList().filterIsInstance<Inet4Address>()
                .mapNotNull { a -> a.hostAddress?.takeIf(::usable)?.let { "${ni.name}:$it" } }
        }
    } catch (_: Exception) { emptyList() }
}
