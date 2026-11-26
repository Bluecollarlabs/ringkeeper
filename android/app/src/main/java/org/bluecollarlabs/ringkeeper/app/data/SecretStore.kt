package org.bluecollarlabs.ringkeeper.app.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Secrets (ring auth key hex, ingest bearer token) wrapped with a non-exportable
 * AES-256-GCM key in the Android Keystore; only ciphertext lands in SharedPreferences.
 * App data is excluded from cloud backup/device transfer (data_extraction_rules.xml).
 *
 * The ring key is the ONLY copy (the ring never reveals it): losing it = factory reset.
 * TODO(roadmap): user-initiated encrypted export of the key for an offline backup.
 * Never log values read from here.
 */
class SecretStore(context: Context) {
    private val prefs = context.getSharedPreferences("secrets", Context.MODE_PRIVATE)

    var ringKeyHex: String?
        get() = get("ring_key")
        set(v) = put("ring_key", v)

    var ingestToken: String?
        get() = get("ingest_token")
        set(v) = put("ingest_token", v)

    private fun put(name: String, value: String?) {
        if (value == null) { prefs.edit().remove(name).apply(); return }
        val c = Cipher.getInstance(TRANSFORM)
        c.init(Cipher.ENCRYPT_MODE, key())
        val ct = c.doFinal(value.toByteArray(Charsets.UTF_8))
        val blob = c.iv + ct
        // commit(): the ring key must be durable before we continue (pairing flow)
        prefs.edit().putString(name, Base64.encodeToString(blob, Base64.NO_WRAP)).commit()
    }

    private fun get(name: String): String? {
        val b64 = prefs.getString(name, null) ?: return null
        val blob = Base64.decode(b64, Base64.NO_WRAP)
        val c = Cipher.getInstance(TRANSFORM)
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob, 0, IV_LEN))
        return String(c.doFinal(blob, IV_LEN, blob.size - IV_LEN), Charsets.UTF_8)
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    private companion object {
        const val ALIAS = "ringkeeper_secrets_v1"
        const val TRANSFORM = "AES/GCM/NoPadding"
        const val IV_LEN = 12
    }
}
