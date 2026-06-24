export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  // Allows to automatically instantiate createClient with right options
  // instead of createClient<Database, { PostgrestVersion: 'XX' }>(URL, KEY)
  __InternalSupabase: {
    PostgrestVersion: "14.5"
  }
  public: {
    Tables: {
      admin_audit: {
        Row: {
          action: string
          actor_email: string | null
          actor_id: string | null
          created_at: string
          id: string
          payload: Json | null
        }
        Insert: {
          action: string
          actor_email?: string | null
          actor_id?: string | null
          created_at?: string
          id?: string
          payload?: Json | null
        }
        Update: {
          action?: string
          actor_email?: string | null
          actor_id?: string | null
          created_at?: string
          id?: string
          payload?: Json | null
        }
        Relationships: []
      }
      binance_keys: {
        Row: {
          api_key_encrypted: string
          api_key_last4: string
          api_secret_encrypted: string
          created_at: string
          id: string
          permissions_note: string | null
          updated_at: string
          user_id: string
        }
        Insert: {
          api_key_encrypted: string
          api_key_last4: string
          api_secret_encrypted: string
          created_at?: string
          id?: string
          permissions_note?: string | null
          updated_at?: string
          user_id: string
        }
        Update: {
          api_key_encrypted?: string
          api_key_last4?: string
          api_secret_encrypted?: string
          created_at?: string
          id?: string
          permissions_note?: string | null
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      diagnostics: {
        Row: {
          audit_status: Database["public"]["Enums"]["audit_status"]
          bar_timestamp: string
          created_at: string
          details: Json | null
          fingerprint_match: boolean | null
          id: string
          model_version: string
          parity_ok: boolean | null
        }
        Insert: {
          audit_status: Database["public"]["Enums"]["audit_status"]
          bar_timestamp: string
          created_at?: string
          details?: Json | null
          fingerprint_match?: boolean | null
          id?: string
          model_version: string
          parity_ok?: boolean | null
        }
        Update: {
          audit_status?: Database["public"]["Enums"]["audit_status"]
          bar_timestamp?: string
          created_at?: string
          details?: Json | null
          fingerprint_match?: boolean | null
          id?: string
          model_version?: string
          parity_ok?: boolean | null
        }
        Relationships: []
      }
      engine_config: {
        Row: {
          capital_allocation_pct: number
          capital_usd: number
          created_at: string
          demo_mode: boolean
          id: string
          is_running: boolean
          leverage: number
          max_daily_loss_usd: number
          max_position_size_usd: number
          mode: string
          updated_at: string
          user_id: string
        }
        Insert: {
          capital_allocation_pct?: number
          capital_usd?: number
          created_at?: string
          demo_mode?: boolean
          id?: string
          is_running?: boolean
          leverage?: number
          max_daily_loss_usd?: number
          max_position_size_usd?: number
          mode?: string
          updated_at?: string
          user_id: string
        }
        Update: {
          capital_allocation_pct?: number
          capital_usd?: number
          created_at?: string
          demo_mode?: boolean
          id?: string
          is_running?: boolean
          leverage?: number
          max_daily_loss_usd?: number
          max_position_size_usd?: number
          mode?: string
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      engine_status: {
        Row: {
          created_at: string
          current_position: string
          id: string
          last_heartbeat: string | null
          message: string | null
          status: string
          updated_at: string
          user_id: string
        }
        Insert: {
          created_at?: string
          current_position?: string
          id?: string
          last_heartbeat?: string | null
          message?: string | null
          status?: string
          updated_at?: string
          user_id: string
        }
        Update: {
          created_at?: string
          current_position?: string
          id?: string
          last_heartbeat?: string | null
          message?: string | null
          status?: string
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      model_versions: {
        Row: {
          created_at: string
          fingerprint: Json
          id: string
          is_active: boolean
          status: string
          storage_key: string
          training_window: string | null
          version_label: string
        }
        Insert: {
          created_at?: string
          fingerprint?: Json
          id?: string
          is_active?: boolean
          status?: string
          storage_key: string
          training_window?: string | null
          version_label: string
        }
        Update: {
          created_at?: string
          fingerprint?: Json
          id?: string
          is_active?: boolean
          status?: string
          storage_key?: string
          training_window?: string | null
          version_label?: string
        }
        Relationships: []
      }
      open_positions: {
        Row: {
          atr: number | null
          bars_held: number | null
          created_at: string
          current_stop: number | null
          entry: number | null
          entry_t: string | null
          id: string
          prob: number | null
          setup_name: string | null
          side: string | null
          sl: number | null
          threshold: number | null
          tp: number | null
          trade_id: string
          unrealized_pnl_rate: number | null
          updated_at: string
          user_id: string
        }
        Insert: {
          atr?: number | null
          bars_held?: number | null
          created_at?: string
          current_stop?: number | null
          entry?: number | null
          entry_t?: string | null
          id?: string
          prob?: number | null
          setup_name?: string | null
          side?: string | null
          sl?: number | null
          threshold?: number | null
          tp?: number | null
          trade_id: string
          unrealized_pnl_rate?: number | null
          updated_at?: string
          user_id: string
        }
        Update: {
          atr?: number | null
          bars_held?: number | null
          created_at?: string
          current_stop?: number | null
          entry?: number | null
          entry_t?: string | null
          id?: string
          prob?: number | null
          setup_name?: string | null
          side?: string | null
          sl?: number | null
          threshold?: number | null
          tp?: number | null
          trade_id?: string
          unrealized_pnl_rate?: number | null
          updated_at?: string
          user_id?: string
        }
        Relationships: []
      }
      positions: {
        Row: {
          entry_price: number
          entry_ts: string
          id: string
          last_price: number | null
          model_version: string
          quantity: number
          side: Database["public"]["Enums"]["trade_side"]
          state: Database["public"]["Enums"]["position_state"]
          stop_loss: number | null
          take_profit: number | null
          unrealized_pnl: number | null
          updated_at: string
        }
        Insert: {
          entry_price: number
          entry_ts: string
          id?: string
          last_price?: number | null
          model_version: string
          quantity?: number
          side: Database["public"]["Enums"]["trade_side"]
          state?: Database["public"]["Enums"]["position_state"]
          stop_loss?: number | null
          take_profit?: number | null
          unrealized_pnl?: number | null
          updated_at?: string
        }
        Update: {
          entry_price?: number
          entry_ts?: string
          id?: string
          last_price?: number | null
          model_version?: string
          quantity?: number
          side?: Database["public"]["Enums"]["trade_side"]
          state?: Database["public"]["Enums"]["position_state"]
          stop_loss?: number | null
          take_profit?: number | null
          unrealized_pnl?: number | null
          updated_at?: string
        }
        Relationships: []
      }
      signals: {
        Row: {
          bar_timestamp: string
          created_at: string
          diagnostics: Json | null
          emitted: boolean
          id: string
          ml_probability: number | null
          ml_threshold: number | null
          model_version: string
          price: number | null
          setup: string | null
          side: Database["public"]["Enums"]["trade_side"]
          veto_reason: string | null
          vetoed: boolean
        }
        Insert: {
          bar_timestamp: string
          created_at?: string
          diagnostics?: Json | null
          emitted?: boolean
          id?: string
          ml_probability?: number | null
          ml_threshold?: number | null
          model_version: string
          price?: number | null
          setup?: string | null
          side: Database["public"]["Enums"]["trade_side"]
          veto_reason?: string | null
          vetoed?: boolean
        }
        Update: {
          bar_timestamp?: string
          created_at?: string
          diagnostics?: Json | null
          emitted?: boolean
          id?: string
          ml_probability?: number | null
          ml_threshold?: number | null
          model_version?: string
          price?: number | null
          setup?: string | null
          side?: Database["public"]["Enums"]["trade_side"]
          veto_reason?: string | null
          vetoed?: boolean
        }
        Relationships: []
      }
      system_status: {
        Row: {
          active_version: string | null
          audit_status: Database["public"]["Enums"]["audit_status"]
          control_flag: Database["public"]["Enums"]["control_flag"]
          id: number
          last_bar_ts: string | null
          last_heartbeat: string | null
          updated_at: string
          worker_state: Database["public"]["Enums"]["worker_state"]
        }
        Insert: {
          active_version?: string | null
          audit_status?: Database["public"]["Enums"]["audit_status"]
          control_flag?: Database["public"]["Enums"]["control_flag"]
          id?: number
          last_bar_ts?: string | null
          last_heartbeat?: string | null
          updated_at?: string
          worker_state?: Database["public"]["Enums"]["worker_state"]
        }
        Update: {
          active_version?: string | null
          audit_status?: Database["public"]["Enums"]["audit_status"]
          control_flag?: Database["public"]["Enums"]["control_flag"]
          id?: number
          last_bar_ts?: string | null
          last_heartbeat?: string | null
          updated_at?: string
          worker_state?: Database["public"]["Enums"]["worker_state"]
        }
        Relationships: []
      }
      trades: {
        Row: {
          created_at: string
          entry_price: number
          entry_ts: string
          exit_price: number | null
          exit_reason: string | null
          exit_ts: string | null
          id: string
          model_version: string
          pnl: number | null
          pnl_pct: number | null
          quantity: number
          setup: string | null
          side: Database["public"]["Enums"]["trade_side"]
        }
        Insert: {
          created_at?: string
          entry_price: number
          entry_ts: string
          exit_price?: number | null
          exit_reason?: string | null
          exit_ts?: string | null
          id?: string
          model_version: string
          pnl?: number | null
          pnl_pct?: number | null
          quantity?: number
          setup?: string | null
          side: Database["public"]["Enums"]["trade_side"]
        }
        Update: {
          created_at?: string
          entry_price?: number
          entry_ts?: string
          exit_price?: number | null
          exit_reason?: string | null
          exit_ts?: string | null
          id?: string
          model_version?: string
          pnl?: number | null
          pnl_pct?: number | null
          quantity?: number
          setup?: string | null
          side?: Database["public"]["Enums"]["trade_side"]
        }
        Relationships: []
      }
      user_roles: {
        Row: {
          created_at: string
          id: string
          role: Database["public"]["Enums"]["app_role"]
          user_id: string
        }
        Insert: {
          created_at?: string
          id?: string
          role: Database["public"]["Enums"]["app_role"]
          user_id: string
        }
        Update: {
          created_at?: string
          id?: string
          role?: Database["public"]["Enums"]["app_role"]
          user_id?: string
        }
        Relationships: []
      }
      user_signals: {
        Row: {
          bar_closed_now: boolean | null
          bar_time: string
          closed_reason: string | null
          created_at: string
          id: string
          ml_accept: boolean | null
          ml_prob: number | null
          ml_threshold: number | null
          opened: string | null
          position_after: string | null
          position_before: string | null
          rule_reason: string | null
          rule_side: number | null
          trade_id: string | null
          user_id: string
          valid_next_entry: boolean | null
        }
        Insert: {
          bar_closed_now?: boolean | null
          bar_time: string
          closed_reason?: string | null
          created_at?: string
          id?: string
          ml_accept?: boolean | null
          ml_prob?: number | null
          ml_threshold?: number | null
          opened?: string | null
          position_after?: string | null
          position_before?: string | null
          rule_reason?: string | null
          rule_side?: number | null
          trade_id?: string | null
          user_id: string
          valid_next_entry?: boolean | null
        }
        Update: {
          bar_closed_now?: boolean | null
          bar_time?: string
          closed_reason?: string | null
          created_at?: string
          id?: string
          ml_accept?: boolean | null
          ml_prob?: number | null
          ml_threshold?: number | null
          opened?: string | null
          position_after?: string | null
          position_before?: string | null
          rule_reason?: string | null
          rule_side?: number | null
          trade_id?: string | null
          user_id?: string
          valid_next_entry?: boolean | null
        }
        Relationships: []
      }
      user_trades: {
        Row: {
          atr: number | null
          bars_held: number | null
          created_at: string
          entry: number | null
          entry_t: string | null
          exit: number | null
          exit_reason: string | null
          exit_t: string | null
          final_stop: number | null
          id: string
          net_pnl_rate: number | null
          prob: number | null
          round_trip_cost: number | null
          setup_name: string | null
          side: string | null
          signal_t: string | null
          sl: number | null
          threshold: number | null
          tp: number | null
          trade_id: string | null
          user_id: string
        }
        Insert: {
          atr?: number | null
          bars_held?: number | null
          created_at?: string
          entry?: number | null
          entry_t?: string | null
          exit?: number | null
          exit_reason?: string | null
          exit_t?: string | null
          final_stop?: number | null
          id?: string
          net_pnl_rate?: number | null
          prob?: number | null
          round_trip_cost?: number | null
          setup_name?: string | null
          side?: string | null
          signal_t?: string | null
          sl?: number | null
          threshold?: number | null
          tp?: number | null
          trade_id?: string | null
          user_id: string
        }
        Update: {
          atr?: number | null
          bars_held?: number | null
          created_at?: string
          entry?: number | null
          entry_t?: string | null
          exit?: number | null
          exit_reason?: string | null
          exit_t?: string | null
          final_stop?: number | null
          id?: string
          net_pnl_rate?: number | null
          prob?: number | null
          round_trip_cost?: number | null
          setup_name?: string | null
          side?: string | null
          signal_t?: string | null
          sl?: number | null
          threshold?: number | null
          tp?: number | null
          trade_id?: string | null
          user_id?: string
        }
        Relationships: []
      }
      worker_logs: {
        Row: {
          context: Json | null
          created_at: string
          id: string
          level: string
          message: string
        }
        Insert: {
          context?: Json | null
          created_at?: string
          id?: string
          level?: string
          message: string
        }
        Update: {
          context?: Json | null
          created_at?: string
          id?: string
          level?: string
          message?: string
        }
        Relationships: []
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      decrypt_binance_keys_for: {
        Args: { _user_id: string }
        Returns: {
          api_key: string
          api_secret: string
        }[]
      }
      delete_binance_keys: { Args: never; Returns: undefined }
      get_my_binance_key_info: {
        Args: never
        Returns: {
          api_key_last4: string
          created_at: string
          permissions_note: string
          updated_at: string
        }[]
      }
      has_role: {
        Args: {
          _role: Database["public"]["Enums"]["app_role"]
          _user_id: string
        }
        Returns: boolean
      }
      is_admin: { Args: never; Returns: boolean }
      save_binance_keys: {
        Args: { _api_key: string; _api_secret: string; _note?: string }
        Returns: undefined
      }
    }
    Enums: {
      app_role: "admin" | "viewer"
      audit_status: "PASS" | "WARN" | "FAIL" | "UNKNOWN"
      control_flag: "RUNNING" | "PAUSED" | "STOPPED"
      position_state: "OPEN" | "CLOSED"
      trade_side: "LONG" | "SHORT"
      worker_state: "OK" | "DEGRADED" | "HALTED" | "UNKNOWN"
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  public: {
    Enums: {
      app_role: ["admin", "viewer"],
      audit_status: ["PASS", "WARN", "FAIL", "UNKNOWN"],
      control_flag: ["RUNNING", "PAUSED", "STOPPED"],
      position_state: ["OPEN", "CLOSED"],
      trade_side: ["LONG", "SHORT"],
      worker_state: ["OK", "DEGRADED", "HALTED", "UNKNOWN"],
    },
  },
} as const
