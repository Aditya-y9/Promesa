import { GoogleGenerativeAI } from '@google/generative-ai';
import { SettingsService } from './settings';
import { getTokenUsage, reportTokenUsage, estimateCostUsd } from './trace';

export interface LLMResult {
  text: string;
  tokensIn: number;
  tokensOut: number;
  costEstimateUsd: number;
  model: string;
}

/**
 * Direct Google Gemini integration (no LangChain wrapper to keep deps minimal).
 * Temperature always 0 — deterministic guardrails.
 * Tracks token usage so the TraceService can build cost + audit trails.
 */
export class LLMService {
  private client: GoogleGenerativeAI | undefined;
  private modelName = 'gemini-1.5-pro';

  constructor(private settings: SettingsService) {}

  async getClient(): Promise<GoogleGenerativeAI> {
    if (!this.client) {
      const key = await this.settings.getApiKey();
      if (!key) throw new Error('Gemini API key not set. Add it in the Settings tab.');
      this.client = new GoogleGenerativeAI(key.trim());
    }
    return this.client;
  }

  async clear(): Promise<void> {
    this.client = undefined;
  }

  async invoke(systemPrompt: string, userPrompt: string): Promise<string> {
    const r = await this.invokeDetailed(systemPrompt, userPrompt);
    return r.text;
  }

  /**
   * invokeDetailed returns token usage + cost alongside the text, so callers can feed
   * the TraceService. This is how we get real token/cost numbers for audit & budgets.
   */
  async invokeDetailed(systemPrompt: string, userPrompt: string): Promise<LLMResult> {
    const client = await this.getClient();
    const model = client.getGenerativeModel({
      model: this.modelName,
      generationConfig: {
        temperature: 0,
        topP: 1,
        topK: 1,
      },
    });
    const result = await model.generateContent([
      { text: systemPrompt },
      { text: userPrompt },
    ]);
    const response = result.response;
    const text = response.text();
    const usage = response.usageMetadata;
    const tokensIn = usage?.promptTokenCount ?? this.approxTokens(systemPrompt + userPrompt);
    const tokensOut = usage?.candidatesTokenCount ?? this.approxTokens(text);
    // Report to the shared aggregate (used by trace/session summary).
    reportTokenUsage(this.modelName, tokensIn, tokensOut);
    return {
      text,
      tokensIn,
      tokensOut,
      costEstimateUsd: estimateCostUsd(this.modelName, tokensIn, tokensOut),
      model: this.modelName,
    };
  }

  /** Fallback approximation only when usageMetadata is absent. */
  private approxTokens(s: string): number {
    return Math.ceil(s.length / 4);
  }
}

/** Re-export the shared aggregate for convenience (kept in trace.ts). */
export { getTokenUsage };
