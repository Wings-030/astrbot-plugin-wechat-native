# SPEC: WeChat Native Image Recognition

**Repository:** https://github.com/neozhu/astrbot-plugin-wechat-native  
**Native:** agent-wechat-native v1.3.3  
**AstrBot:** >=4.29.3,<5  
**Language:** Python 3.12+  
**Status:** Ready for Implementation  
**Version:** 2.1 — Includes approved v0.2.5 quote fallback and separate group mention

## 1. Objective

Extend the existing WeChat Native platform adapter to support receiving and analyzing images through AstrBot's built-in multimodal AI pipeline.

The implementation must be minimal, reliable and backward-compatible.

**Core principle:**

The adapter only handles image detection, Native API retrieval and construction of AstrBot `Image` components. AstrBot handles model selection, image encoding, multimodal requests, conversation context and AI responses.

Do not introduce a separate LLM client, Vision API, Bridge service or independent processing framework.

## 2. User Experience

### Scenario A — Private Chat: Direct Image

User sends a picture directly to the bot.

Expected behavior:

1. Detect the incoming image.
2. Download it through the Native Media API.
3. Convert it to an AstrBot `Image` component.
4. Submit it to the existing AstrBot message pipeline.
5. Return the AI response to WeChat.

If no user prompt is provided, use:

`请描述这张图片的内容。`

An empty image-message body must not prevent processing.

### Scenario B — Private Chat: Quoted Image

User quotes a previously received image and asks:

`帮我看看这张图`

Expected behavior:

- Resolve the image embedded in the quote.
- Preserve the user's question.
- Download and forward the image to AstrBot.
- Return the AI response.

### Scenario C — Group Chat: Quoted Image + @Bot

User interaction:

1. Someone sends an image in a WeChat group.
2. Another member quotes that image.
3. The member sends `@机器人 帮我分析这张图`.

Expected behavior:

- Validate the existing group permissions.
- Verify the genuine WeChat @mention.
- Retrieve the quoted image.
- Forward the image and question to AstrBot.
- Reply using the existing message-sending mechanism.

If the message only contains a genuine `@机器人` mention and no additional text, use the default image-analysis prompt.

### Scenario D — Group Image Without @Mention

Do not download or analyze images automatically.

Ordinary group text processing must continue to follow the existing `group_reply_mode` setting.

### Scenario E — Unsupported Content

This release supports images only.

Do not automatically process:

- Stickers or emoji packages
- PDF/DOCX/XLSX attachments
- Voice messages
- Videos
- Link-card thumbnails
- Images embedded inside forwarded-chat bundles

Keep existing behavior for these messages.

### Scenario F — Group Image Followed by a Separate @Mention

Record only the identifiers of incoming live images in allowed groups. A genuine @mention
with an image-analysis prompt may select that sender's latest image from the same group
within 120 seconds. An otherwise empty @mention may use the same image and default prompt.
Keep at most 256 sources in memory, clear them on shutdown, and remove recalled images.
Do not download images until requested. Ordinary text and summary commands retain their
existing behavior. If an explicit image request has no matching image, ask the user to
resend or quote it without submitting an incomplete request to the model.

## 3. Configuration

Add **only one user-facing configuration option**.

| Setting | Type | Default | Description |
|---|---|---|---|
| `enable_media` | Boolean | `true` | Enable image recognition |

AstrBot configuration description:

**启用图片识别**

`支持私聊图片识别，以及群聊中引用图片并 @机器人进行分析。`

All other operational settings must remain internal constants.

```python
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MEDIA_DOWNLOAD_TIMEOUT = 60
MAX_CONCURRENT_MEDIA_TASKS = 3
DEFAULT_IMAGE_PROMPT = "请描述这张图片的内容。"
```

Do not introduce additional media configuration options.

When `enable_media=False`, retain the existing text-only behavior.

Existing configuration files must continue to load without modification.

## 4. Architecture

```text
WeChat Message
      |
      v
agent-wechat-native
      |
      v
Existing WeChatNativeAdapter
      |
      +-- Existing permission checks
      +-- Image / quote detection
      |
      v
Native Media API
      |
      +-- Query image assets
      +-- Download image
      +-- Validate content
      |
      v
AstrBot Image.fromBytes(image_bytes)
      |
      v
Existing AstrBot Event Pipeline
      |
      v
Configured AI Model
      |
      v
Existing WeChat Text Reply
```

**Do not create:**

- A new Bridge service
- A custom multimodal LLM adapter
- A separate message queue
- An additional database
- An independent image-processing pipeline
- A complex provider-capability detection system

Reuse the existing adapter and AstrBot framework wherever possible.

## 5. Native Media API

Use the existing authenticated Native v1 API.

Base path:

`/v1/accounts/{accountId}`

Required endpoints:

```http
GET /messages/{messageId}
GET /messages/{messageId}/media
POST /messages/{messageId}/media-transfers
GET /jobs/{jobId}
GET /media/{assetId}/content
```

Transfer request example:

```json
{
  "messageId": "MESSAGE_ID",
  "quality": "original"
}
```

### Image resolution

For direct images, use the incoming message's Native `id`.

For quoted images, use the **current quote-reply message's Native `id`** to retrieve embedded quote media.

Do not substitute `quote.serverId` for a Native message ID.

Approved v0.2.5 exception: only if that lookup returns HTTP 409 `media_metadata_invalid`,
resolve a unique previously recorded original Native ID using the same thread and quoted
server ID. Validate account, thread, both message IDs, sender, image type and recall state
before and after downloading, revalidate the triggering quote before event delivery, and
recheck the original before replying. Other Native errors retain existing handling. The
fallback and initial attempt share the existing media deadline and deduplication rules.

Use the actual Native v1.3.3 message structure:

- Direct image: `type == 3`
- Quote reply: `type == 49` and `appMessageType == 57`
- Quoted image: inspect `quote.type`, `quote.embeddedImage` and available media metadata

Message metadata must be validated instead of assuming that quoted-image content is always downloadable.

### Image quality

Prefer a compatible image with sufficient resolution for OCR and screenshot analysis.

Simple selection rules:

1. Prefer an available `original` or `standard` image.
2. If only a thumbnail is cached but a higher-quality variant is available for transfer, attempt one higher-quality transfer.
3. If the higher-quality image is unavailable, use a valid thumbnail as fallback.
4. Do not send raw WXGF bytes to AstrBot. Use a supported JPEG preview when available.

The Native API's `best` selection may return an already-cached thumbnail. Do not assume it always returns the highest available resolution.

### Download behavior

- Reuse existing HTTP authentication.
- Require Native Token scopes: `read + send + media`.
- Reuse an existing active media job when possible.
- Use bounded asynchronous HTTP streaming.
- Check Content-Length when available.
- Stop downloading immediately when the byte limit is exceeded.
- Respect the 60-second transfer deadline.
- Handle pending, unavailable, expired and unsupported states.

Do not log image bytes or authentication tokens.

## 6. AstrBot Integration

Use the existing AstrBot image component.

```python
from astrbot.api.message_components import Image, Plain

image_component = Image.fromBytes(image_bytes)

message.message.append(image_component)
```

Use the existing `convert_message()` and event-dispatch flow.

### Message construction rules

1. Preserve existing `At`, `Reply` and `Plain` components.
2. Append the downloaded image as **one top-level `Image` component**.
3. Do not also place the same image in `Reply.chain`.
4. Preserve the user's actual text.
5. If the prompt is empty, apply the default prompt.
6. Keep `message.message_str` synchronized with the final prompt.
7. When a quoted image is successfully resolved, avoid misleading placeholder text such as `不支持此引用消息的内容`.

Example:

```text
Message components:
    At(bot)
    Reply(quoted_message)
    Plain("帮我分析这张图片")
    Image(image_bytes)

message_str:
    "帮我分析这张图片"
```

The image must be prepared before delivering the event to AstrBot.

### LLM handling

AstrBot already supports:

- Image message components
- Multimodal model requests
- Image encoding
- Vision-capable providers
- Model fallback and image-caption mechanisms
- Conversation context management

Do not duplicate these features.

Do not tightly couple the adapter to AstrBot's internal model configuration or attempt to reimplement model selection.

If the existing AstrBot configuration cannot process images, fail clearly rather than deliberately forwarding only an image placeholder to a text-only model.

Validate the behavior with both Vision-capable and text-only model configurations during integration testing.

## 7. Permissions and Group Rules

Reuse existing authorization logic.

### Private chats

Image processing requires:

- `enable_media=True`
- `enable_private_reply=True`
- Sender passes the existing `allowed_users` policy
- Message is incoming and live
- Message has not been recalled

### Group chats

Image processing requires:

- `enable_media=True`
- Group passes the existing `allowed_groups` policy
- `group_reply_mode != "disabled"`
- A verified `mentions.state == "known"` and `mentions.self == true`
- The message is a supported quoted-image request

Even when `group_reply_mode == "all"`, image analysis must require a genuine @mention.

**Group summary commands take precedence.**

If a message is a group-summary request, execute the existing summary behavior without downloading a quoted image.

Do not change how existing group text messages are handled.

## 8. Recall and Error Handling

Preserve existing recall protection.

### Triggering message recalled

If the image message or quote-reply request itself is recalled:

- Stop processing.
- Do not call the LLM.
- Do not send an error or AI response.

### Referenced image unavailable

If the triggering message remains valid but the quoted image has expired or cannot be retrieved:

- Do not forward the incomplete image request to the LLM.
- Reply with a concise error when permitted by existing recall and quote-validity rules.
- If the existing quote invalidation protection suppresses replies, preserve that protection.

### Error examples

| Condition | Response |
|---|---|
| Image unavailable | 图片无法读取，请重新发送。 |
| Referenced image expired | 引用的图片已失效，请重新发送。 |
| Image exceeds limit | 图片太大，暂时无法处理。 |
| Missing Native media scope | 图片读取功能未正确配置，请联系管理员。 |
| Unsupported format | 暂不支持这种图片格式。 |

Do not expose internal exceptions to WeChat users.

## 9. Concurrency and Deduplication

Keep the existing SQLite `StateStore`.

Do not add a separate persistence framework.

### Required behavior

- Register the received message through the existing deduplication mechanism before starting image download.
- Perform downloads asynchronously so the SSE listener can continue receiving events.
- Use a small semaphore to limit concurrent media jobs.
- Track outstanding tasks and cancel them during adapter shutdown.
- Before delivering the enriched event, recheck the stored message recall state.
- Preserve existing send idempotency and quote invalidation checks.
- On restart, do not automatically replay a partially processed image request or generate duplicate replies.

Reuse the current message states as far as possible.

If a pending media operation cannot safely resume after restart, mark it for review rather than adding complex recovery infrastructure.

The design should prioritize **at-most-once AI event delivery** over automatic retry of uncertain operations.

## 10. Image Validation

Accept only supported image data.

Minimum supported formats:

- JPEG
- PNG
- WebP
- GIF, when a compatible representation is available

Validate actual content signatures rather than trusting extensions.

Reject:

- Raw unsupported WXGF content
- HTML/XML disguised as an image
- Unsupported binary data
- Oversized files
- Corrupted images

For WXGF, prefer Native's supported JPEG preview if one is available.

Avoid additional image conversion dependencies unless clearly necessary.

Prefer `Image.fromBytes()` for validated images. There is no need to introduce temporary files for ordinary image processing.

## 11. Files to Modify

Keep changes focused on the existing project.

| File | Change |
|---|---|
| `native_client.py` | Add media metadata, transfer-job and binary-download methods |
| `adapter.py` | Add image eligibility and asynchronous retrieval |
| `event.py` | Insert `Image` into the incoming message |
| `config.py` | Add one `enable_media` setting |
| `README.md` | Document image support and `media` token permission |
| Existing tests | Extend coverage for image and quote scenarios |

A small `media_handler.py` helper is acceptable if it significantly improves clarity, but it is not mandatory.

Do not introduce unnecessary abstractions or refactor unrelated code.

## 12. Tests and Acceptance Criteria

### Private chat

- [ ] Direct JPEG/PNG messages reach AstrBot as real `Image` components.
- [ ] Empty image-message bodies are accepted.
- [ ] Default prompt is correctly applied.
- [ ] Private quoted-image analysis works.
- [ ] Existing private text messages are unaffected.

### Group chat

- [ ] Quoted image + genuine @mention triggers image analysis.
- [ ] Quoted image without @mention does not trigger media download.
- [ ] `group_reply_mode="disabled"` blocks image processing.
- [ ] `group_reply_mode="all"` does not automatically analyze group images.
- [ ] Group-summary commands continue to work.
- [ ] Text quotes continue to work.

### Media and AI

- [ ] Correct Native message ID is used for quoted-image media.
- [ ] Native media jobs are handled correctly.
- [ ] Higher-resolution variants are preferred over thumbnails.
- [ ] Raw WXGF bytes are not forwarded to the model.
- [ ] Exactly one image reaches AstrBot for each user-selected image.
- [ ] Real image content reaches a configured Vision-capable model.
- [ ] Unsupported model configurations do not produce fabricated image descriptions.

### Reliability

- [ ] Duplicate SSE events do not cause duplicate AI requests.
- [ ] Triggering-message recall silently cancels processing.
- [ ] Invalidated quotes are handled using existing protections.
- [ ] Image download timeout does not block unrelated messages.
- [ ] Oversized and invalid images are safely rejected.
- [ ] Missing `media` scope produces a clear configuration error.
- [ ] Existing tests continue to pass.

Use mocked Native HTTP responses and synthetic image fixtures for automated tests.

Perform one manual end-to-end test using Native v1.3.3 and an AstrBot Vision model.

## 13. Out of Scope

Do not implement the following in this release:

- PDF/DOCX/XLSX document processing
- Voice or video understanding
- Automatic OCR service integration
- Sticker analysis
- AI image generation or outbound image sending
- Custom Vision API clients
- Additional configuration settings
- New Docker services or databases

These features can be considered separately after image recognition is stable.

## 14. Instructions for Codex

Implement this specification using **minimal, targeted changes**.

Before editing, inspect the existing repository and verify the relevant Native v1.3.3 and AstrBot APIs.

Implementation priorities:

1. Support private image recognition.
2. Support group quoted-image recognition with genuine @mention.
3. Preserve existing text, quote, authorization and recall behavior.
4. Add focused tests and update documentation.

### Engineering constraints

- Prefer existing framework capabilities over new code.
- Avoid speculative abstractions and overengineering.
- Do not rewrite unrelated modules.
- Do not introduce additional configuration options.
- Do not implement custom LLM integration.
- Reuse the existing SQLite state and event-delivery architecture.
- Keep image downloading asynchronous, bounded and cancellable.
- Preserve backward compatibility.

If a requirement is already implemented by AstrBot, use it rather than duplicating it.

If Native v1.3.3 cannot provide a particular image variant, use a safe supported fallback instead of building a new media subsystem.

### Definition of Done

The implementation is complete when:

1. A user sends an image in private WeChat and receives an AI-generated description.
2. A group member quotes an image, genuinely @mentions the bot, and receives an AI analysis.
3. All existing text functionality continues working.
4. Only one new setting, `enable_media`, is visible.
5. No separate LLM, Bridge, database or complex framework has been introduced.
6. Automated tests and real WeChat smoke tests pass.

**Final principle: Keep it simple. Let Native retrieve the image, let AstrBot understand it, and let the existing adapter return the answer.**

## References

- Plugin: https://github.com/neozhu/astrbot-plugin-wechat-native
- Native Media API: https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/api.mdx
- Native image quality: https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/quality.mdx
- Native message types: https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/how-it-works/message-types.mdx
- AstrBot Image component: https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/components.py
- AstrBot multimodal pipeline: https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/astr_main_agent/request.py
