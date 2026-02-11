import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from Backend.config import Telegram
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.metadata import metadata
from Backend import db

# Pattern to extract chat_id and message_id from a link
TELEGRAM_LINK_PATTERN = r"https://t\.me/(?:c/)?([^/]+)/(\d+)"

@Client.on_message(filters.command("batch") & filters.private & CustomFilters.owner, group=10)
async def batch_index_handler(client: Client, message: Message):
    """
    Index a range of messages from a channel.
    Usage: /batch https://t.me/c/123/100 https://t.me/c/123/200
    """
    try:
        # 1. Validate Input
        if len(message.command) < 3:
            await message.reply_text(
                "⚠️ **Usage:**\n`/batch <start_link> <end_link>`\n\n"
                "Example:\n`/batch https://t.me/c/123456/100 https://t.me/c/123456/500`"
            )
            return

        start_link = message.command[1]
        end_link = message.command[2]

        start_match = re.search(TELEGRAM_LINK_PATTERN, start_link)
        end_match = re.search(TELEGRAM_LINK_PATTERN, end_link)

        if not start_match or not end_match:
            await message.reply_text("❌ Invalid link format.")
            return

        start_chat_ref, start_id = start_match.groups()
        end_chat_ref, end_id = end_match.groups()

        if start_chat_ref != end_chat_ref:
            await message.reply_text("❌ Start and End links must be from the same chat.")
            return

        # 2. Resolve Chat ID (Fixes the Public Username Crash)
        try:
            # If it looks like a private channel ID (digits), prepopulate -100
            if start_chat_ref.isdigit():
                lookup_id = int(f"-100{start_chat_ref}")
            else:
                lookup_id = start_chat_ref
            
            # Resolve to get the real Integer ID (Handles Usernames & IDs)
            chat = await client.get_chat(lookup_id)
            chat_id = chat.id
            
            # Calculate DB Channel ID (Positive Integer for DB)
            # Safely remove -100 prefix if present
            channel_db_id = int(str(chat_id).replace("-100", "", 1))
            
        except Exception as e:
            await message.reply_text(f"❌ Could not access chat. Make sure I am an admin there.\nError: {e}")
            return

        # Ensure correct range order
        start_id = int(start_id)
        end_id = int(end_id)
        if end_id < start_id:
            start_id, end_id = end_id, start_id

        total_messages = end_id - start_id + 1

        # 3. Notify User
        status_msg = await message.reply_text(
            f"🔄 **Starting Batch Indexing**\n"
            f"📂 Chat ID: `{chat_id}`\n"
            f"🔢 Range: `{start_id}` - `{end_id}`\n"
            f"📊 Total: `{total_messages}` messages"
        )

        success_count = 0
        processed_count = 0
        
        # 4. Iterate and Process in Batches (Much Faster)
        # We fetch 200 messages at a time to reduce API calls
        CHUNK_SIZE = 200
        
        for i in range(start_id, end_id + 1, CHUNK_SIZE):
            try:
                # Calculate the batch range
                batch_end = min(i + CHUNK_SIZE, end_id + 1)
                message_ids = list(range(i, batch_end))
                
                # Fetch messages
                try:
                    messages = await client.get_messages(chat_id, message_ids)
                except FloodWait as e:
                    LOGGER.warning(f"FloodWait: Sleeping {e.value}s")
                    await asyncio.sleep(e.value)
                    messages = await client.get_messages(chat_id, message_ids)
                except Exception as e:
                    LOGGER.error(f"Failed to fetch batch {i}-{batch_end}: {e}")
                    continue

                # Process each message in the batch
                for msg in messages:
                    if not msg: 
                        continue # Skip empty/deleted messages

                    try:
                        # Check for video content
                        if msg.video or (msg.document and (msg.document.mime_type or "").startswith("video/")):
                            file = msg.video or msg.document
                            raw_caption = msg.caption or file.file_name or ""
                            
                            # --- Metadata Extraction Logic ---
                            clean_name = clean_filename(raw_caption)
                            size = get_readable_file_size(file.file_size)
                            title = remove_urls(raw_caption)
                            if not title.endswith(('.mkv', '.mp4')):
                                title += '.mkv'

                            # Extract Metadata
                            metadata_info = await metadata(clean_name, channel_db_id, msg.id)

                            if metadata_info:
                                # Insert into Database
                                await db.insert_media(
                                    metadata_info, 
                                    channel=channel_db_id, 
                                    msg_id=msg.id, 
                                    size=size, 
                                    name=title
                                )
                                success_count += 1
                        
                        processed_count += 1
                        
                    except Exception as inner_e:
                        LOGGER.error(f"Error processing msg {msg.id}: {inner_e}")

                # Update Status every chunk
                if processed_count % CHUNK_SIZE == 0 or processed_count >= total_messages:
                    try:
                        await status_msg.edit_text(
                            f"🔄 **Batch Indexing...**\n"
                            f"⚙️ Processed: `{processed_count}/{total_messages}`\n"
                            f"✅ Indexed: `{success_count}`"
                        )
                    except Exception:
                        pass # Ignore edit errors (floodwait on edit)

            except Exception as e:
                LOGGER.error(f"Batch loop error: {e}")
                await asyncio.sleep(5)

        # 5. Final Report
        await status_msg.edit_text(
            f"✅ **Batch Processing Complete**\n\n"
            f"🔢 Scanned: `{processed_count}`\n"
            f"✅ Successfully Indexed: `{success_count}`"
        )

    except Exception as e:
        LOGGER.error(f"Batch command failed: {e}")
        await message.reply_text(f"❌ Error: {e}")