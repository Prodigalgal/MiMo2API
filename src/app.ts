import path from "node:path";
import { fileURLToPath } from "node:url";
import cors from "@fastify/cors";
import staticPlugin from "@fastify/static";
import Fastify, { type FastifyInstance } from "fastify";
import { RenewalScheduler } from "./accounts/renewal-scheduler.js";
import { ConfigStore } from "./config/store.js";
import { requireAdmin } from "./core/auth.js";
import { ApiError, asApiError } from "./core/errors.js";
import { ModelService } from "./models/service.js";
import { CompletionService } from "./protocol/completion.js";
import { ProxyPool } from "./proxy/pool.js";
import { RegistrationService } from "./registration/service.js";
import { ResponseRepository } from "./responses/repository.js";
import { ResponsesService } from "./responses/service.js";
import { adminRoutes } from "./routes/admin.js";
import { chatRoutes } from "./routes/chat.js";
import { modelRoutes } from "./routes/models.js";
import { responseRoutes } from "./routes/responses.js";
import { UsageRepository } from "./usage/repository.js";
import { AccountRequestCoordinator } from "./accounts/request-coordinator.js";

export interface BuildOptions {
  configFile?: string;
  databaseFile?: string;
  startScheduler?: boolean;
  logger?: boolean;
}

export async function buildApp(options: BuildOptions = {}): Promise<FastifyInstance> {
  const config = await ConfigStore.open(options.configFile, options.databaseFile);
  const usage = new UsageRepository(config.database);
  const accountRequests = new AccountRequestCoordinator();
  const completion = new CompletionService(config, usage, accountRequests);
  const responseRepository = new ResponseRepository(config.database);
  const responses = new ResponsesService(config, completion, responseRepository);
  const models = new ModelService(config);
  const renewals = new RenewalScheduler(config, accountRequests);
  const proxy = new ProxyPool(config.snapshot().proxy_pool);
  const registrations = new RegistrationService(config, proxy);
  const app = Fastify({
    logger: options.logger ?? process.env.NODE_ENV !== "test",
    requestTimeout: 0,
    bodyLimit: Number(process.env.MIMO2API_BODY_LIMIT_BYTES ?? 35 * 1024 * 1024),
  });

  await app.register(cors, { origin: true, credentials: true });
  await app.register(staticPlugin, {
    root: path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../web"),
    prefix: "/assets/",
    decorateReply: true,
    wildcard: false,
    index: false,
  });
  await app.register(staticPlugin, {
    root: path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../node_modules/lucide/dist/umd"),
    prefix: "/vendor/lucide/",
    decorateReply: false,
    wildcard: false,
    index: false,
  });

  app.setErrorHandler((error, request, reply) => {
    const apiError = asApiError(error);
    if (apiError.status === 401) {
      reply.header("WWW-Authenticate", apiError.code === "invalid_admin_auth" ? 'Basic realm="MiMo2API"' : "Bearer");
    }
    request.log[apiError.status >= 500 ? "error" : "warn"](error);
    void reply.status(apiError.status === 499 ? 499 : apiError.status).send(apiError.toJSON(request.id));
  });

  app.get("/healthz", async () => ({
    ok: true,
    service: "mimo2api",
    version: "3.0.0",
    database: "sqlite",
    accounts: config.snapshot().mimo_accounts.length,
    inference: completion.capacityStatus(),
    renewals: renewals.status(),
  }));
  app.get("/favicon.ico", async (_request, reply) => reply.status(204).send());
  app.get("/", { preHandler: requireAdmin(config) }, async (_request, reply) => reply.sendFile("index.html"));
  app.get("/admin", { preHandler: requireAdmin(config) }, async (_request, reply) => reply.sendFile("index.html"));
  app.get("/index.html", { preHandler: requireAdmin(config) }, async (_request, reply) => reply.sendFile("index.html"));

  await app.register(modelRoutes(config, models));
  await app.register(chatRoutes(config, completion));
  await app.register(responseRoutes(config, responses));
  await app.register(adminRoutes(config, { renewals, proxy, registrations, responses: responseRepository, usage }));

  app.addHook("onClose", async () => {
    await renewals.stop();
    await responses.stop();
    await registrations.stop();
    await proxy.close();
    completion.close();
    config.database.close();
  });

  if (options.startScheduler !== false && process.env.MIMO2API_RENEW_ENABLED !== "false") renewals.start();
  void models.refresh();
  return app;
}

export { ApiError };
