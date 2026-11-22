export default function (pi) {
  pi.registerProvider("commandcode", {
    name: "CommandCode GOAT",
    baseUrl: "https://api.commandcode.ai/provider/v1",
    api: "openai-completions",
    apiKey: "$COMMANDCODE_API_KEY",
    models: [{
      id: "deepseek/deepseek-v4.1-flash",
      name: "DeepSeek V4.1 Flash",
      reasoning: true,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 1000000,
      maxTokens: 32768,
      compat: { supportsDeveloperRole: false, maxTokensField: "max_tokens" },
    }],
  });
}
