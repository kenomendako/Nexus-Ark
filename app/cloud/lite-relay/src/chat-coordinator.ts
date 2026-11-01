import {
  commitCompletedMessage,
  markProviderStarted,
  markRequestTerminal,
  reserveMessage,
  type CommitMessageInput,
  type ReserveMessageInput,
} from "./storage";
import type { NormalizedStream, RequestStatus } from "./types";

export interface CoordinateMessageInput {
  db: D1Database;
  reservation: ReserveMessageInput;
  providerStartedAt: string;
  finalizedAt: string;
  invoke: () => Promise<NormalizedStream>;
  buildCommit: (stream: NormalizedStream) => CommitMessageInput;
}

export interface CoordinateMessageResult {
  invoked: boolean;
  status: RequestStatus;
}

export async function coordinateMessage(input: CoordinateMessageInput): Promise<CoordinateMessageResult> {
  const reservation = await reserveMessage(input.db, input.reservation);
  if (!reservation.created) {
    return { invoked: false, status: reservation.request.status };
  }

  if (!(await markProviderStarted(input.db, input.reservation.clientMessageId, input.providerStartedAt))) {
    throw new Error("message_request_start_conflict");
  }

  try {
    const stream = await input.invoke();
    if (stream.routing_violation) {
      await markRequestTerminal(input.db, input.reservation.clientMessageId, "failed_known", input.finalizedAt);
      return { invoked: true, status: "failed_known" };
    }
    if (stream.terminal_status === "completed") {
      await commitCompletedMessage(input.db, input.buildCommit(stream));
      return { invoked: true, status: "completed" };
    }
    const status = stream.terminal_status === "partial" ? "partial" : "failed_known";
    await markRequestTerminal(input.db, input.reservation.clientMessageId, status, input.finalizedAt);
    return { invoked: true, status };
  } catch {
    await markRequestTerminal(input.db, input.reservation.clientMessageId, "outcome_unknown", input.finalizedAt);
    return { invoked: true, status: "outcome_unknown" };
  }
}
