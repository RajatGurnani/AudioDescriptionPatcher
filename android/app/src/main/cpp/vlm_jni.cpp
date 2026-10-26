// JNI bridge: one image + prompt -> short text, via llama.cpp libmtmd.
// Kotlin side: describe/VlmDescriber.kt. One context per handle; calls on
// the same handle must not overlap (VlmDescriber serializes them).
#include <jni.h>
#include <android/log.h>
#include <chrono>
#include <string>
#include <vector>

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"

#define TAG "adpvlm"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

namespace {

struct Vlm {
    llama_model * model = nullptr;
    llama_context * lctx = nullptr;
    mtmd_context * mctx = nullptr;
    llama_sampler * smpl = nullptr;
    int n_batch = 512;
    // timings of the last describe() call, ms: eval prompt+image, generate
    long long t_eval_ms = 0, t_gen_ms = 0, n_gen = 0, n_prompt = 0;
};

void log_cb(ggml_log_level level, const char * text, void *) {
    if (level >= GGML_LOG_LEVEL_WARN) {
        __android_log_print(level == GGML_LOG_LEVEL_ERROR ? ANDROID_LOG_ERROR : ANDROID_LOG_WARN,
                            TAG, "%s", text);
    }
}

long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::string jstr(JNIEnv * env, jstring s) {
    const char * c = env->GetStringUTFChars(s, nullptr);
    std::string out(c);
    env->ReleaseStringUTFChars(s, c);
    return out;
}

void free_vlm(Vlm * v) {
    if (!v) return;
    if (v->smpl) llama_sampler_free(v->smpl);
    if (v->mctx) mtmd_free(v->mctx);
    if (v->lctx) llama_free(v->lctx);
    if (v->model) llama_model_free(v->model);
    delete v;
}

// chat template of the model with the media marker in the user turn
std::string format_prompt(const Vlm * v, const std::string & user) {
    const char * tmpl = llama_model_chat_template(v->model, nullptr);
    std::string content = std::string(mtmd_default_marker()) + "\n" + user;
    llama_chat_message msg{"user", content.c_str()};
    std::vector<char> buf(content.size() * 2 + 512);
    int n = llama_chat_apply_template(tmpl, &msg, 1, true, buf.data(), (int32_t) buf.size());
    if (n > (int) buf.size()) {
        buf.resize(n);
        n = llama_chat_apply_template(tmpl, &msg, 1, true, buf.data(), (int32_t) buf.size());
    }
    if (n < 0) return content;  // unknown template: raw prompt
    return std::string(buf.data(), n);
}

}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_com_fuzzypyjamas_adpatcher_describe_VlmDescriber_nativeInit(
        JNIEnv * env, jclass, jstring jmodel, jstring jmmproj, jint threads,
        jint n_ctx, jint image_max_tokens) {
    llama_log_set(log_cb, nullptr);
    mtmd_helper_log_set(log_cb, nullptr);
    llama_backend_init();

    auto * v = new Vlm();
    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;
    v->model = llama_model_load_from_file(jstr(env, jmodel).c_str(), mp);
    if (!v->model) { LOGE("model load failed"); free_vlm(v); return 0; }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = (uint32_t) n_ctx;
    cp.n_batch = (uint32_t) v->n_batch;
    cp.n_threads = threads;
    cp.n_threads_batch = threads;
    v->lctx = llama_init_from_model(v->model, cp);
    if (!v->lctx) { LOGE("context init failed"); free_vlm(v); return 0; }

    mtmd_context_params mcp = mtmd_context_params_default();
    mcp.use_gpu = false;
    mcp.n_threads = threads;
    mcp.print_timings = false;
    mcp.warmup = false;
    if (image_max_tokens > 0) mcp.image_max_tokens = image_max_tokens;
    v->mctx = mtmd_init_from_file(jstr(env, jmmproj).c_str(), v->model, mcp);
    if (!v->mctx) { LOGE("mmproj load failed"); free_vlm(v); return 0; }

    // greedy: reproducible lines for the same frame
    v->smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(v->smpl, llama_sampler_init_greedy());
    LOGI("init ok: threads=%d n_ctx=%d image_max_tokens=%d", threads, n_ctx, image_max_tokens);
    return reinterpret_cast<jlong>(v);
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_fuzzypyjamas_adpatcher_describe_VlmDescriber_nativeDescribe(
        JNIEnv * env, jclass, jlong handle, jbyteArray jrgb, jint w, jint h,
        jstring jprompt, jint max_tokens) {
    auto * v = reinterpret_cast<Vlm *>(handle);
    if (!v) return nullptr;
    v->t_eval_ms = v->t_gen_ms = v->n_gen = v->n_prompt = 0;

    jsize n_bytes = env->GetArrayLength(jrgb);
    if (n_bytes != w * h * 3) { LOGE("rgb size %d != %dx%dx3", n_bytes, w, h); return nullptr; }
    std::vector<unsigned char> rgb(n_bytes);
    env->GetByteArrayRegion(jrgb, 0, n_bytes, reinterpret_cast<jbyte *>(rgb.data()));

    llama_memory_clear(llama_get_memory(v->lctx), true);
    llama_sampler_reset(v->smpl);

    std::string prompt = format_prompt(v, jstr(env, jprompt));
    mtmd_bitmap * bmp = mtmd_bitmap_init((uint32_t) w, (uint32_t) h, rgb.data());
    mtmd_input_text text{prompt.c_str(), prompt.size(), true, true};
    mtmd_input_chunks * chunks = mtmd_input_chunks_init();
    const mtmd_bitmap * bmps[] = {bmp};

    long long t0 = now_ms();
    int32_t rc = mtmd_tokenize(v->mctx, chunks, &text, bmps, 1);
    llama_pos n_past = 0;
    if (rc == 0) {
        v->n_prompt = (long long) mtmd_helper_get_n_tokens(chunks);
        rc = mtmd_helper_eval_chunks(v->mctx, v->lctx, chunks, 0, 0, v->n_batch, true, &n_past);
    }
    mtmd_input_chunks_free(chunks);
    mtmd_bitmap_free(bmp);
    long long t1 = now_ms();
    v->t_eval_ms = t1 - t0;
    if (rc != 0) { LOGE("eval failed rc=%d", rc); return nullptr; }

    const llama_vocab * vocab = llama_model_get_vocab(v->model);
    llama_batch batch = llama_batch_init(1, 0, 1);
    std::string out;
    char piece[256];
    for (int i = 0; i < max_tokens; i++) {
        llama_token tok = llama_sampler_sample(v->smpl, v->lctx, -1);
        llama_sampler_accept(v->smpl, tok);
        if (llama_vocab_is_eog(vocab, tok)) break;
        int n = llama_token_to_piece(vocab, tok, piece, sizeof(piece), 0, false);
        if (n > 0) out.append(piece, n);
        v->n_gen++;
        // explicit position: M-RoPE models (Qwen-VL) advance n_past by
        // the image's position count, not its token count
        batch.n_tokens = 1;
        batch.token[0] = tok;
        batch.pos[0] = n_past++;
        batch.n_seq_id[0] = 1;
        batch.seq_id[0][0] = 0;
        batch.logits[0] = true;
        if (llama_decode(v->lctx, batch) != 0) { LOGE("decode failed"); break; }
    }
    llama_batch_free(batch);
    v->t_gen_ms = now_ms() - t1;
    LOGI("describe: prompt=%lld tok eval=%lld ms, gen=%lld tok %lld ms",
         v->n_prompt, v->t_eval_ms, v->n_gen, v->t_gen_ms);
    return env->NewStringUTF(out.c_str());
}

extern "C" JNIEXPORT jlongArray JNICALL
Java_com_fuzzypyjamas_adpatcher_describe_VlmDescriber_nativeLastTimings(
        JNIEnv * env, jclass, jlong handle) {
    auto * v = reinterpret_cast<Vlm *>(handle);
    jlong vals[4] = {v ? v->t_eval_ms : 0, v ? v->t_gen_ms : 0, v ? v->n_prompt : 0, v ? v->n_gen : 0};
    jlongArray arr = env->NewLongArray(4);
    env->SetLongArrayRegion(arr, 0, 4, vals);
    return arr;
}

extern "C" JNIEXPORT void JNICALL
Java_com_fuzzypyjamas_adpatcher_describe_VlmDescriber_nativeFree(JNIEnv *, jclass, jlong handle) {
    free_vlm(reinterpret_cast<Vlm *>(handle));
}
