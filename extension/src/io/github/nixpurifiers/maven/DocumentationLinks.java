// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.xml.parsers.DocumentBuilderFactory;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

/** Project captured metadata onto the doclet's native offlineLinks without changing public hrefs. */
final class DocumentationLinks {
  private DocumentationLinks() {}

  private static String base(String value) {
    while (value.endsWith("/")) value = value.substring(0, value.length() - 1);
    return value;
  }

  private static String expand(String value, MavenProject project) {
    for (int round = 0; round < 10; round++) {
      String previous = value;
      for (String key : project.getProperties().stringPropertyNames()) {
        value = value.replace("${" + key + "}", project.getProperties().getProperty(key));
      }
      if (previous.equals(value)) break;
    }
    return value;
  }

  private static Xpp3Dom child(Xpp3Dom parent, String name) {
    Xpp3Dom result = parent.getChild(name);
    if (result == null) {
      result = new Xpp3Dom(name);
      parent.addChild(result);
    }
    return result;
  }

  private static void project(Xpp3Dom config, MavenProject project, Map<String, String> metadata) {
    Xpp3Dom links = config.getChild("links");
    if (links != null) {
      for (int i = links.getChildCount() - 1; i >= 0; i--) {
        String value = links.getChild(i).getValue();
        if (value != null && metadata.containsKey(base(expand(value, project)))) links.removeChild(i);
      }
      // The execution's projected list must not merge the original remote list back in.
      links.setAttribute("combine.self", "override");
    }
    Xpp3Dom offline = child(config, "offlineLinks");
    for (Map.Entry<String, String> entry : metadata.entrySet()) {
      Xpp3Dom selected = null;
      for (Xpp3Dom link : offline.getChildren("offlineLink")) {
        Xpp3Dom url = link.getChild("url");
        if (url != null && entry.getKey().equals(base(expand(url.getValue(), project)))) selected = link;
      }
      if (selected == null) {
        selected = new Xpp3Dom("offlineLink");
        child(selected, "url").setValue(entry.getKey());
        offline.addChild(selected);
      }
      child(selected, "location").setValue(entry.getValue());
    }
    offline.setAttribute("combine.self", "override");
  }

  static void apply(MavenSession session) throws Exception {
    File repository = session.getRequest().getLocalRepositoryPath();
    if (repository == null) return;
    File index = new File(repository, "downloads/javadoc/index.xml");
    if (!index.isFile()) return;
    DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
    factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
    Element document = factory.newDocumentBuilder().parse(index).getDocumentElement();
    if (!"javadoc".equals(document.getTagName()) || !"1".equals(document.getAttribute("version"))) {
      throw new IOException("unsupported locked documentation index");
    }
    for (MavenProject project : session.getProjects()) {
      String key = DocumentationCapture.projectKey(project);
      for (Node node = document.getFirstChild(); node != null; node = node.getNextSibling()) {
        if (!(node instanceof Element) || !"project".equals(node.getNodeName())) continue;
        Element recorded = (Element) node;
        if (!key.equals(recorded.getAttribute("id"))) continue;
        for (Node item = recorded.getFirstChild(); item != null; item = item.getNextSibling()) {
          if (!(item instanceof Element) || !"execution".equals(item.getNodeName())) continue;
          Element execution = (Element) item;
          Map<String, String> metadata = new LinkedHashMap<String, String>();
          for (Node value = execution.getFirstChild(); value != null; value = value.getNextSibling()) {
            if (!(value instanceof Element) || !"link".equals(value.getNodeName())) continue;
            Element link = (Element) value;
            File directory = new File(repository, link.getAttribute("directory")).getCanonicalFile();
            if (!directory.toPath().startsWith(repository.getCanonicalFile().toPath())) {
              throw new IOException("locked documentation directory escapes the repository");
            }
            int files = 0;
            for (Node part = link.getFirstChild(); part != null; part = part.getNextSibling()) {
              if (!(part instanceof Element) || !"source".equals(part.getNodeName())) continue;
              Element source = (Element) part;
              String name = source.getAttribute("name");
              if (!("element-list".equals(name) || "package-list".equals(name))) {
                throw new IOException("unsupported locked documentation metadata file");
              }
              byte[] contents = Files.readAllBytes(new File(directory, name).toPath());
              StringBuilder digest = new StringBuilder();
              for (byte b : MessageDigest.getInstance("SHA-256").digest(contents)) digest.append(String.format("%02x", b & 255));
              if (!digest.toString().equals(source.getAttribute("sha256"))) {
                throw new IOException("locked documentation metadata changed: " + link.getAttribute("url"));
              }
              files++;
            }
            if (files == 0) throw new IOException("locked documentation metadata is absent");
            metadata.put(base(link.getAttribute("url")), directory.getAbsolutePath());
          }
          for (Plugin plugin : project.getBuildPlugins()) {
            if (!"maven-javadoc-plugin".equals(plugin.getArtifactId())) continue;
            PluginExecution selected = null;
            for (PluginExecution candidate : plugin.getExecutions()) {
              if (execution.getAttribute("id").equals(candidate.getId())) selected = candidate;
            }
            if (selected == null) {
              selected = new PluginExecution();
              selected.setId(execution.getAttribute("id"));
              plugin.addExecution(selected);
            }
            Xpp3Dom config = selected.getConfiguration() instanceof Xpp3Dom
                ? new Xpp3Dom((Xpp3Dom) selected.getConfiguration()) : new Xpp3Dom("configuration");
            if (plugin.getConfiguration() instanceof Xpp3Dom) {
              config = Xpp3Dom.mergeXpp3Dom(config, new Xpp3Dom((Xpp3Dom) plugin.getConfiguration()));
            }
            project(config, project, metadata);
            selected.setConfiguration(config);
            plugin.flushExecutionMap();
          }
        }
      }
    }
  }
}
