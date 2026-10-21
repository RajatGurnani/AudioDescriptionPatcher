package com.fuzzypyjamas.adpatcher

import android.content.Context
import android.net.Uri
import android.util.Log
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

/**
 * On-disk cache of onset envelopes - the Android twin of adpatch.py's
 * cached_envelope(). A repeat run on the same file (retry, other output
 * options, resume after the analysis was lost) skips the decode, which is
 * most of the analysis time.
 *
 * Content-keyed (size + first/last MiB), so a renamed copy or a fresh
 * SAF grant to the same file still hits. ~3 MB per 2 h of audio; pruned
 * oldest-first beyond [LIMIT] in the app's cacheDir (Android may clear
 * it any time - it's only a cache).
 */
object EnvelopeCache {
    private const val TAG = "ADPatcher"
    /** Bump whenever AudioEngine's envelope output changes. 2 = the
     *  count-anchored decode from the splice-sync fix (e26ade7). */
    private const val VERSION = 2
    private const val LIMIT = 200L shl 20
    private const val EDGE = 1 shl 20

    private fun dir(context: Context) =
        File(context.cacheDir, "envelopes").apply { mkdirs() }

    /** Content key, or null if the file can't be read (no caching). */
    private fun key(context: Context, uri: Uri): String? = try {
        context.contentResolver.openFileDescriptor(uri, "r")!!.use { pfd ->
            FileInputStream(pfd.fileDescriptor).channel.use { ch ->
                val size = ch.size()
                val md = MessageDigest.getInstance("SHA-1")
                md.update(("v$VERSION:${AudioEngine.SR}:${AudioEngine.HOP}:" +
                    "$size").toByteArray())
                fun digest(from: Long) {
                    val buf = ByteBuffer.allocate(
                        minOf(EDGE.toLong(), size - from).toInt())
                    ch.position(from)
                    while (buf.hasRemaining() && ch.read(buf) > 0) { }
                    buf.flip(); md.update(buf)
                }
                digest(0)
                if (size > 2L * EDGE) digest(size - EDGE)
                md.digest().joinToString("") { "%02x".format(it) }
            }
        }
    } catch (e: Exception) {
        Log.i(TAG, "envelope cache: can't key $uri (${e.message})")
        null
    }

    /**
     * The envelope of [uri] from cache, or [compute]d (with progress)
     * and stored. Returns (envelope, cacheHit).
     */
    fun get(
        context: Context, uri: Uri, onProgress: (Float) -> Unit,
        compute: ((Float) -> Unit) -> FloatArray
    ): Pair<FloatArray, Boolean> {
        val k = key(context, uri)
        val file = k?.let { File(dir(context), "$it.f32") }
        if (file != null && file.isFile) {
            try {
                val bytes = file.readBytes()
                val bb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
                val env = FloatArray(bytes.size / 4) { bb.float }
                file.setLastModified(System.currentTimeMillis())   // LRU
                onProgress(1f)
                return env to true
            } catch (e: Exception) {
                Log.i(TAG, "envelope cache: unreadable entry, recomputing")
            }
        }
        val env = compute(onProgress)
        if (file != null) {
            try {
                val bb = ByteBuffer.allocate(env.size * 4)
                    .order(ByteOrder.LITTLE_ENDIAN)
                env.forEach { bb.putFloat(it) }
                val tmp = File(file.path + ".tmp")
                tmp.writeBytes(bb.array())
                tmp.renameTo(file)
                prune(context)
            } catch (e: Exception) {
                Log.i(TAG, "envelope cache: write failed (${e.message})")
            }
        }
        return env to false
    }

    private fun prune(context: Context) {
        val files = dir(context).listFiles()
            ?.filter { it.name.endsWith(".f32") }
            ?.sortedBy { it.lastModified() } ?: return
        var total = files.sumOf { it.length() }
        for (f in files) {
            if (total <= LIMIT) break
            total -= f.length()
            f.delete()
        }
    }
}
