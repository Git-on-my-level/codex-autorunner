/**
 * The ONLY module in this workstream that imports grammY.
 *
 * It is loaded lazily (dynamic import from index.ts's loop.start()) and only
 * when Telegram is enabled and a token is present, so importing the surface —
 * as every test does — never constructs a Bot and never touches the network.
 * Everything it does is delegate: updates go to handlers.ts (rows only), and
 * outbound traffic arrives as a `TelegramSendFn` handed to the deliverer.
 */
import { Bot } from "grammy";
import { handleCallback, handleCommand, routeIncomingMessage, type HandlerDeps } from "./handlers.ts";
import { editMarkup, toReplyMarkup } from "./render.ts";
import type { MessageSpec, TelegramSendFn, TelegramTarget } from "./types.ts";

export interface BotHandle {
  start(): void;
  stop(): Promise<void>;
  send: TelegramSendFn;
}

export function createBot(token: string, deps: HandlerDeps): BotHandle {
  const bot = new Bot(token);
  const defaultChat = deps.config.telegram.chat_id;

  /** Only the configured chat may drive CAR. Everything else is dropped. */
  const allowed = (chatId: string | number | undefined): boolean =>
    !defaultChat || String(chatId ?? "") === defaultChat;

  bot.on("callback_query:data", async (ctx) => {
    if (!allowed(ctx.chat?.id)) {
      await ctx.answerCallbackQuery({ text: "not your bot" });
      return;
    }
    const message = ctx.callbackQuery.message as { message_id?: number; text?: string } | undefined;
    const input: Parameters<typeof handleCallback>[1] = { data: ctx.callbackQuery.data };
    if (ctx.from?.id) input.from = String(ctx.from.id);
    if (message?.message_id !== undefined) input.messageId = String(message.message_id);
    if (message?.text) input.messageText = message.text;
    let ack: { text: string; alert?: boolean };
    try {
      ack = await handleCallback(deps, input);
    } catch (err) {
      deps.store.audit("daemon", "telegram.callback_error", "callback", ctx.callbackQuery.data, {
        error: String(err),
      });
      ack = { text: "Something went wrong — it's in the audit log.", alert: true };
    }
    await ctx.answerCallbackQuery({ text: ack.text.slice(0, 200), show_alert: ack.alert === true });
  });

  bot.on("message:text", async (ctx) => {
    if (!allowed(ctx.chat.id)) return;
    const text = ctx.message.text;
    try {
      if (text.startsWith("/")) {
        const space = text.indexOf(" ");
        const command = space < 0 ? text : text.slice(0, space);
        const args = space < 0 ? "" : text.slice(space + 1);
        const from = ctx.from?.id !== undefined ? String(ctx.from.id) : undefined;
        const reply = await handleCommand(deps, from ? { command, args, from } : { command, args });
        await ctx.reply(reply);
        return;
      }
      const msg: Parameters<typeof routeIncomingMessage>[1] = {
        text,
        messageId: String(ctx.message.message_id),
      };
      if (ctx.message.reply_to_message?.message_id !== undefined) {
        msg.replyToMessageId = String(ctx.message.reply_to_message.message_id);
      }
      if (ctx.message.message_thread_id !== undefined) {
        msg.threadId = String(ctx.message.message_thread_id);
      }
      if (ctx.from?.id !== undefined) msg.from = String(ctx.from.id);
      const outcome = await routeIncomingMessage(deps, msg);
      if (outcome.kind === "delivered") await ctx.reply("→ delivered to the agent.");
      else if (outcome.kind === "queued") await ctx.reply("→ queued for the agent.");
      else if (outcome.kind === "note") await ctx.reply("📝 noted.");
    } catch (err) {
      deps.store.audit("daemon", "telegram.message_error", "message", String(ctx.message.message_id), {
        error: String(err),
      });
    }
  });

  const send: TelegramSendFn = async (target: TelegramTarget, spec: MessageSpec) => {
    const chatId = target.chat_id || defaultChat;
    if (!chatId) throw new Error("telegram: no chat_id configured");
    const replyMarkup = toReplyMarkup(spec);

    if (target.edit_message_id) {
      // editMarkup is always sent: see its doc comment for why omitting it is a bug.
      await bot.api.editMessageText(chatId, Number(target.edit_message_id), spec.text, {
        reply_markup: editMarkup(spec),
      });
      return { message_id: target.edit_message_id };
    }

    let threadId = target.thread_id ?? undefined;
    let createdThread: string | undefined;
    if (!threadId && target.create_topic) {
      const topic = await bot.api.createForumTopic(chatId, target.create_topic);
      threadId = String(topic.message_thread_id);
      createdThread = threadId;
    }

    const sent = await bot.api.sendMessage(chatId, spec.text, {
      ...(threadId ? { message_thread_id: Number(threadId) } : {}),
      ...(target.reply_to_message_id
        ? { reply_parameters: { message_id: Number(target.reply_to_message_id), allow_sending_without_reply: true } }
        : {}),
      ...(spec.disable_notification ? { disable_notification: true } : {}),
      ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
    });
    return {
      message_id: String(sent.message_id),
      ...(createdThread ? { thread_id: createdThread } : {}),
    };
  };

  return {
    start() {
      // Long polling: no inbound port, nothing to expose.
      void bot.start({ drop_pending_updates: false }).catch((err: unknown) => {
        deps.store.audit("daemon", "telegram.poll_error", "daemon", "card", { error: String(err) });
      });
    },
    async stop() {
      await bot.stop();
    },
    send,
  };
}
