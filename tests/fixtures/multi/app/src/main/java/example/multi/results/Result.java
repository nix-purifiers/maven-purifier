// SPDX-License-Identifier: MIT
package example.multi.results;

public final class Result {
  private Result() {}

  public static String value() {
    return "retained";
  }
}
