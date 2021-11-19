package com.github.rtisma.toolbox.file_renamer.cli.util;

import lombok.NonNull;
import lombok.RequiredArgsConstructor;
import lombok.SneakyThrows;
import lombok.val;

import static com.github.lalyos.jfiglet.FigletFont.convertOneLine;
import static java.util.Arrays.stream;

@RequiredArgsConstructor
public class ProjectBanner {

  /** Other fonts can be found at http://www.figlet.org/examples.html */
  private static final String BANNER_FONT_LOC = "/banner-fonts/slant.flf";

  @NonNull private final String applicationName;
  // Example: "${Ansi.GREEN} "
  @NonNull private final String linePrefix;
  @NonNull private final String linePostfix;


  @SneakyThrows
  public String generateBannerText() {
    val text = convertOneLine("classpath:" + BANNER_FONT_LOC, applicationName);
    val sb = new StringBuilder();
    stream(text.split("\n"))
        .forEach(t -> sb.append(linePrefix).append(t).append(linePostfix).append("\n"));
    return sb.toString();
  }
}
